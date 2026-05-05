"""Serialize a ``RoleAgent.build_prompt()`` dict into a string for engines.

``RoleAgent`` returns a structured dict so the test layer can inspect
it and the engine adapter layer can format it however its CLI expects.
``serialize_prompt()`` produces the canonical text representation: a
section-based document an engine adapter can pipe to ``codex run`` or
``claude run`` etc.

The serializer:
  - reads the SQLite-backed ``TaskRecord`` for goal / acceptance / plan / constraints
    (the dict only carries task_id; the rest comes from the task store)
  - composes the selected instruction layers from the dict
  - surfaces ``last_rejection`` as context for retry prompts
  - surfaces ``recovery_trigger`` for recovery agents
  - finishes with the ``litehive agent report`` verdict instructions

"""

from pathlib import Path
from typing import Any

from pydantic import ValidationError
import yaml

from litehive.domain.common import PipelineState, TaskStage, pipeline_stage_key
from litehive.domain.task import TaskRecord
from litehive.state.records import get_task
from litehive.tasks.activity import load_task_activity


SECTION_SEP = "\n"


def serialize_prompt(
    prompt: dict[str, Any],
    task_record: TaskRecord | None,
    workspace_root: Path | None = None,
) -> str:
    """Render the prompt dict + task record into the engine-facing string.

    ``task_record`` is optional so tests don't need to construct one;
    if ``None``, the goal/acceptance/plan/constraints sections are
    omitted with placeholders. ``workspace_root`` is optional and used
    for two things: (1) fall back to ``get_task()`` if the caller
    didn't already resolve the TaskRecord, and (2) load the task's
    activity history so the next agent visit sees previous stage
    verdicts.
    """
    if task_record is None and workspace_root is not None:
        task_record = get_task(workspace_root, prompt["task_id"])

    activity = prompt.get("activity") or []
    if not activity and workspace_root is not None and task_record is not None:
        activity = _load_task_activity_history(workspace_root, task_record)

    sections: list[str] = []
    sections.append(_header_section(prompt, task_record))
    sections.append(_instructions_section(prompt))
    sections.append(_goal_section(task_record))
    sections.append(_acceptance_criteria_section(task_record))
    sections.append(_plan_section(task_record))
    sections.append(_constraints_section(task_record))

    last_rejection = prompt.get("last_rejection")
    if last_rejection:
        sections.append(_last_rejection_section(last_rejection))

    if (prompt.get("stage_retry") or 0) > 0:
        prior_work = _prior_work_section(prompt.get("last_report") or {}, last_rejection)
        if prior_work:
            sections.append(prior_work)

    recovery_trigger = prompt.get("recovery_trigger")
    if recovery_trigger:
        sections.append(_recovery_trigger_section(recovery_trigger, prompt))

    recovery_execution_root = prompt.get("recovery_execution_root")
    if recovery_execution_root:
        sections.append(_recovery_execution_root_section(prompt))

    failed_subagent_diagnostics = prompt.get("failed_subagent_diagnostics")
    if failed_subagent_diagnostics:
        sections.append(_failed_subagent_diagnostics_section(failed_subagent_diagnostics))

    recovery_history = prompt.get("recovery_history")
    if recovery_history:
        sections.append(_recovery_history_section(recovery_history))

    failed_run_history = prompt.get("failed_run_history")
    if failed_run_history:
        sections.append(_failed_run_history_section(failed_run_history))

    repeated_recovery_fingerprint = prompt.get("repeated_recovery_fingerprint")
    if repeated_recovery_fingerprint:
        sections.append(_repeated_recovery_fingerprint_section(repeated_recovery_fingerprint))

    scope_analysis = prompt.get("scope_analysis")
    if scope_analysis:
        sections.append(_scope_analysis_section(scope_analysis))

    test_failure_attribution = prompt.get("test_failure_attribution")
    if test_failure_attribution:
        sections.append(_test_failure_attribution_section(test_failure_attribution))

    conflict_files = prompt.get("conflict_files")
    if conflict_files:
        sections.append(_merge_conflict_section(conflict_files, prompt.get("merge_attempt")))

    if prompt.get("nudge"):
        sections.append(_nudge_section(prompt))

    if activity:
        sections.append(
            _activity_section(
                activity,
                current_stage=prompt.get("stage"),
                last_rejection=last_rejection,
            )
        )

    runner_hooks = prompt.get("runner_hooks") or []
    if runner_hooks:
        sections.append(_runner_hooks_section(prompt.get("stage"), runner_hooks))

    sections.append(_verdict_instructions_section(prompt))
    return (SECTION_SEP * 2).join(s for s in sections if s).strip() + "\n"


def _load_task_activity_history(workspace_root: Path, task_record: TaskRecord) -> list[dict[str, Any]]:
    """Read the task's persisted activity entries."""
    try:
        activity_entries = load_task_activity(workspace_root, task_record)
    except (OSError, ValidationError, yaml.YAMLError):
        return []
    return [
        {
            "role": entry.role,
            "stage": entry.stage,
            "verdict": entry.verdict,
            "verdict_classification": entry.verdict_classification,
            "message": entry.message,
        }
        for entry in activity_entries
    ]


# ── section builders ────────────────────────────────────────────────────


def _header_section(prompt: dict[str, Any], task_record: TaskRecord | None) -> str:
    """Identify the task and pipeline coordinates so the agent knows which run it is on."""
    lines = [
        f"Task: {prompt['task_id']}" + (f" — {task_record.title}" if task_record is not None else ""),
        f"Stage: {prompt['stage']}",
        f"Role: {prompt['role']}",
        f"Pipeline mode: {prompt['pipeline_mode']}",
    ]
    retry = prompt.get("stage_retry") or 0
    if retry:
        lines.append(f"Stage retry attempt: {retry}")
    return "\n".join(lines)


def _instructions_section(prompt: dict[str, Any]) -> str:
    """Render the role/profile/attempt instruction layers the prompt builder selected for this stage."""
    layers = prompt.get("instruction_layers") or []
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
    """State the task goal; falls back to the title when no goal is defined so the section is never empty."""
    if task_record is None:
        return "Goal:\n(task record not loaded)"
    return f"Goal:\n{task_record.goal or task_record.title}"


def _acceptance_criteria_section(task_record: TaskRecord | None) -> str:
    """Render the acceptance bullets the accepting stage will check this work against."""
    if task_record is None or not task_record.acceptance_criteria:
        return "Acceptance criteria:\n- (none defined)"
    bullets = "\n".join(f"- {c}" for c in task_record.acceptance_criteria)
    return f"Acceptance criteria:\n{bullets}"


def _plan_section(task_record: TaskRecord | None) -> str:
    """Render the plan steps grooming committed to so later stages execute against the same plan."""
    if task_record is None or not task_record.plan:
        return "Plan:\n- (no plan)"
    bullets = "\n".join(f"- {step}" for step in task_record.plan)
    return f"Plan:\n{bullets}"


def _constraints_section(task_record: TaskRecord | None) -> str:
    """Render task-level constraints; supplies a default scope reminder when none are recorded."""
    if task_record is None or not task_record.constraints:
        return "Constraints:\n- Keep changes scoped to the task."
    bullets = "\n".join(f"- {c}" for c in task_record.constraints)
    return f"Constraints:\n{bullets}"


def _last_rejection_section(rejection: dict[str, Any]) -> str:
    """Surface the prior reject verdict as first-class context so the retry agent answers it directly."""
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


def _recovery_trigger_section(recovery_trigger: dict[str, Any], prompt: dict[str, Any]) -> str:
    """Tell the recovery agent which failure shape kicked the task out of the normal pipeline."""
    lines = ["Recovery trigger (what sent the task into recovery):"]
    for key, value in recovery_trigger.items():
        lines.append(f"- {key}: {value}")
    explanation = prompt.get("recovery_failure_explanation")
    if explanation:
        lines.append(f"- recovery_failure_explanation: {explanation}")
    return "\n".join(lines)


def _recovery_execution_root_section(prompt: dict[str, Any]) -> str:
    """Point the recovery agent at the Litehive source checkout to patch, not the task worktree.

    Recovery exists to fix Litehive infrastructure bugs; without this redirect the agent edits
    the wrong tree.
    """
    lines = ["Recovery execution repo (this recovery turn should patch Litehive here):"]
    lines.append(f"- recovery_execution_root: {prompt.get('recovery_execution_root')}")
    source_path = str(prompt.get("litehive_source_path") or "").strip()
    if source_path:
        lines.append(f"- configured_litehive_source_path: {source_path}")
    config_diagnostic = prompt.get("recovery_config_diagnostic")
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
    """Hand the recovery agent persisted evidence of the failed subagent run instead of asking it to dig."""
    lines = [
        "Failed subagent evidence (DB-backed recovery state):",
        f"- subagent_id: {diagnostics.get('subagent_id') or '-'}",
        f"- role: {diagnostics.get('role') or '-'}",
        f"- engine: {diagnostics.get('engine') or '-'}",
        f"- status: {diagnostics.get('status') or '-'}",
        f"- path: {diagnostics.get('path') or '-'}",
        f"- exit_code: {diagnostics.get('exit_code') if diagnostics.get('exit_code') is not None else '-'}",
        f"- did_produce_output: {'yes' if diagnostics.get('did_produce_output') else 'no'}",
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
    """Pick the first failure-flavored line from stdout/stderr/transcript so diagnostics stay one-liner sized."""
    keywords = ("litehive agent report", "traceback", "error", "failed", "exception")
    for text in texts:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and any(keyword in stripped.lower() for keyword in keywords):
                return _single_line(stripped, limit=240)
    return ""


def _single_line(value: str, limit: int) -> str:
    """Collapse whitespace and truncate so multi-line evidence fits one prompt bullet."""
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _recovery_history_section(recovery_history: list[dict[str, Any]]) -> str:
    """Show the recovery agent prior recoveries on this task so it can spot loops instead of re-trying the same fix."""
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
    """Carry forward stage-failure shape counts that persist across requeue/reset, so retry budgets are visible to the agent."""
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
    """Force the recovery agent to escalate (file a bug, reject) when the same failure fingerprint recurs."""
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
    """Tell the recovery agent whether deleted files look like scope creep or legitimate operator cleanup."""
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
    """Tell the recovery agent whether failing tests touch this task's surface or look pre-existing, so it doesn't blame the SWE for unrelated breakage."""
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
    """List the files the merge stage couldn't auto-resolve so the next agent fixes the conflicts before re-merging."""
    bullets = "\n".join(f"- {f}" for f in conflict_files)
    if merge_attempt is not None:
        extra = f"\nMerge attempt: {merge_attempt}"
    else:
        extra = ""
    return f"Merge conflict files (resolve all of these):\n{bullets}{extra}"


def _nudge_section(prompt: dict[str, Any]) -> str:
    """Re-prompt the agent to actually submit a verdict when its prior turn ended without one."""
    message = str(prompt.get("nudge_message") or "").strip()
    lines = [
        "IMPORTANT: this is a nudge because your prior turn ended without a verdict submission.",
    ]
    if message:
        lines.append(message)
    lines.append(
        "Do not continue exploratory work until you have submitted `litehive agent report` with the correct verdict."
    )
    return "\n".join(lines)


_MESSAGE_CAP = 500
_TRUNCATION_MARKER = "…(truncated)"


def _cap_message(entry: dict[str, Any]) -> dict[str, Any]:
    """Cap an activity entry's message so one verbose verdict can't blow up the prompt size.

    Returns a shallow copy with the message truncated; callers keep the original entry intact.
    """
    msg = entry.get("message", "")
    if len(msg) <= _MESSAGE_CAP:
        return entry
    keep = max(_MESSAGE_CAP - len(_TRUNCATION_MARKER), 0)
    return {**entry, "message": msg[:keep] + _TRUNCATION_MARKER}


def _string_list(value: Any) -> list[str]:
    """Coerce report payload values into a deduplicated string list; tolerates the loose typing of subagent JSON."""
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
    """Render a long list as `a, b, c, +N more` so prior-work bullets stay one prompt line."""
    if len(items) <= limit:
        return separator.join(items)
    shown = separator.join(items[:limit])
    return f"{shown}{separator}+{len(items) - limit} more"




def _entry_sources(entry: dict[str, Any]) -> set[str]:
    """Expand an activity entry's role into the set of source labels last_rejection might use to point at it.

    The activity log records role (`grooming`, `hook`, `recovery`, ...) while last_rejection records
    a `source` field that may be either the role or the generic `agent`/`hook`. This bridges the two.
    """
    sources: set[str] = set()
    explicit_source = str(entry.get("source") or "").strip()
    if explicit_source:
        sources.add(explicit_source)

    role = str(entry.get("role") or "").strip()
    if role:
        sources.add(role)
        if role == "hook":
            sources.add("hook")
        elif role != "recovery":
            sources.add("agent")
    return sources


def _matches_last_rejection(entry: dict[str, Any], last_rejection: dict[str, Any]) -> bool:
    """Detect activity entries that already get rendered in the dedicated last-rejection section, so we don't double-render them."""
    if entry.get("verdict") != "reject":
        return False

    rejection_source = str(last_rejection.get("source") or "").strip()
    if rejection_source and rejection_source not in _entry_sources(entry):
        return False

    rejection_stage = pipeline_stage_key(str(last_rejection.get("raised_at_phase") or "").strip() or None)
    entry_stage = pipeline_stage_key(str(entry.get("stage") or "").strip() or None)
    if rejection_stage and entry_stage and rejection_stage != entry_stage:
        return False

    rejection_reason = str(last_rejection.get("reason") or "")
    message = str(entry.get("message") or "")
    if not rejection_reason:
        return False
    return message == rejection_reason or rejection_reason in message


def _trim_activity_for_prompt(
    activity: list[dict[str, Any]],
    current_stage: str | None,
    last_rejection: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Keep only the activity entries the current stage actually needs.

    Rules:
    - Never include recovery entries with verdict=comment (bookkeeping noise).
    - Never duplicate an entry whose content matches last_rejection (already
      rendered in its own section).
    - Per-stage relevance:
        grooming:     nothing (first stage, no prior context needed)
        implementing: grooming pass (scope) + last reject that sent us back
        testing:      last implementing pass (what SWE claims it did)
        accepting:    last implementing pass + last testing pass
        recovering:   last crash/rejection + last implementing pass
        fallback:     grooming pass + last entry per (stage, verdict)
    - Cap each individual message to 500 chars.
    """
    # Filter out recovery bookkeeping entries.
    activity = [e for e in activity if not (e.get("role") == "recovery" and e.get("verdict") == "comment")]

    if not activity:
        return []

    # Skip entries that duplicate last_rejection.
    if last_rejection:
        activity = [entry for entry in activity if not _matches_last_rejection(entry, last_rejection)]

    def _last_where(**match: str) -> dict[str, Any] | None:
        for e in reversed(activity):
            if all(e.get(k) == v for k, v in match.items()):
                return e
        return None

    kept: list[dict[str, Any]] = []

    if current_stage == TaskStage.GROOMING:
        pass  # no activity context needed

    elif current_stage == TaskStage.IMPLEMENTING:
        g = _last_where(stage=TaskStage.GROOMING.value, verdict="pass")
        if g:
            kept.append(g)
        # On retry, the rejection is rendered in the dedicated last_rejection
        # section; do not repeat older reject entries in the activity section.
        if not last_rejection:
            for e in reversed(activity):
                if e.get("verdict") == "reject" and e.get("stage") in (
                    TaskStage.TESTING.value,
                    TaskStage.ACCEPTING.value,
                    TaskStage.IMPLEMENTING.value,
                ):
                    kept.append(e)
                    break

    elif current_stage == TaskStage.TESTING:
        p = _last_where(stage=TaskStage.IMPLEMENTING.value, verdict="pass")
        if p:
            kept.append(p)

    elif current_stage == TaskStage.ACCEPTING:
        p = _last_where(stage=TaskStage.IMPLEMENTING.value, verdict="pass")
        if p:
            kept.append(p)
        t = _last_where(stage=TaskStage.TESTING.value, verdict="pass")
        if t:
            kept.append(t)

    elif current_stage == PipelineState.RECOVERING:
        p = _last_where(stage=TaskStage.IMPLEMENTING.value, verdict="pass")
        if p:
            kept.append(p)
        # The crash or rejection that triggered recovery
        for e in reversed(activity):
            if e.get("verdict") in ("reject", "blocked"):
                kept.append(e)
                break

    else:
        # Fallback: grooming pass + last per (stage, verdict)
        g = _last_where(stage=TaskStage.GROOMING.value, verdict="pass")
        if g:
            kept.append(g)
        seen: set[tuple[str, str]] = set()
        for e in reversed(activity):
            key = (e.get("stage", "?"), e.get("verdict", "?"))
            if key not in seen:
                seen.add(key)
                kept.append(e)

    # Deduplicate, preserve original order, cap messages
    kept_ids = {id(e) for e in kept}
    return [_cap_message(e) for e in activity if id(e) in kept_ids]


def _activity_section(
    activity: list[dict[str, Any]],
    current_stage: str | None = None,
    last_rejection: dict[str, Any] | None = None,
) -> str:
    """Render the trimmed cross-stage activity log so the current agent sees what earlier stages decided."""
    trimmed = _trim_activity_for_prompt(activity, current_stage, last_rejection)
    if not trimmed:
        return ""
    blocks: list[str] = []
    for entry in trimmed:
        role = entry.get("role", "?")
        stage = entry.get("stage", "?")
        verdict = entry.get("verdict", "comment")
        classification = entry.get("verdict_classification") or entry.get("classification")
        if classification:
            verdict_label = f"{verdict}; classification={classification}"
        else:
            verdict_label = str(verdict)
        message = entry.get("message", "")
        blocks.append(f"[{stage}] {role} ({verdict_label}): {message}")
    return "Task activity:\n" + "\n".join(blocks)


def _runner_hooks_section(stage: str | None, hooks: list[dict[str, Any]]) -> str:
    """Warn the agent which post-stage hooks will gate the verdict, so it runs them locally before submitting."""
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


def _verdict_instructions_section(prompt: dict[str, Any]) -> str:
    """Always-final section that tells the agent the exact CLI invocation and verdict vocabulary for its role.

    The role determines the allowed verdict set; recovery has a wider menu than the regular pipeline roles.
    """
    if prompt.get("role") == "recovery":
        verdicts = "<resume|advance|done|budget_hit|reject>"
    else:
        verdicts = "<pass|reject>"
    if prompt.get("role") == "recovery":
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
    """Map prompt-builder layer keys (`role`, `attempt:fresh`, `swe:md`, ...) to human headings the agent reads."""
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
    """De-snake stage names for the runner-hooks heading; stays generic if no stage is set."""
    if not stage:
        return "this stage"
    return stage.replace("_", " ")
