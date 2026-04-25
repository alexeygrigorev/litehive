"""Serialize a v2 ``RoleAgent.build_prompt()`` dict into a string for engines.

The v2 ``RoleAgent`` returns a structured dict so the test layer can inspect
it and the engine adapter layer can format it however its CLI expects.
``serialize_prompt()`` produces the canonical text representation: a
section-based document an engine adapter can pipe to ``codex run`` or
``claude run`` etc.

The serializer:
  - reads the v1 ``TaskRecord`` for goal / acceptance / plan / constraints
    (the dict only carries task_id; the rest comes from task.yaml)
  - composes the selected instruction layers from the dict
  - surfaces ``last_rejection`` as context for retry prompts
  - surfaces ``recovery_trigger`` for recovery agents
  - finishes with the ``litehive report`` verdict instructions

"""

from pathlib import Path
from typing import Any

from pydantic import ValidationError
import yaml

from litehive.domain.task import TaskRecord
from litehive.state.records import get_task
from litehive.tasks.activity import load_task_activity


SECTION_SEP = "\n"
_RECOVERY_ARTIFACT_HEAD_CHARS = 2000
_RECOVERY_ARTIFACT_TAIL_CHARS = 2000


def serialize_prompt(
    prompt: dict[str, Any],
    *,
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
    if task_record is None:
        return "Goal:\n(task record not loaded)"
    return f"Goal:\n{task_record.goal or task_record.title}"


def _acceptance_criteria_section(task_record: TaskRecord | None) -> str:
    if task_record is None or not task_record.acceptance_criteria:
        return "Acceptance criteria:\n- (none defined)"
    bullets = "\n".join(f"- {c}" for c in task_record.acceptance_criteria)
    return f"Acceptance criteria:\n{bullets}"


def _plan_section(task_record: TaskRecord | None) -> str:
    if task_record is None or not task_record.plan:
        return "Plan:\n- (no plan)"
    bullets = "\n".join(f"- {step}" for step in task_record.plan)
    return f"Plan:\n{bullets}"


def _constraints_section(task_record: TaskRecord | None) -> str:
    if task_record is None or not task_record.constraints:
        return "Constraints:\n- Keep changes scoped to the task."
    bullets = "\n".join(f"- {c}" for c in task_record.constraints)
    return f"Constraints:\n{bullets}"


def _last_rejection_section(rejection: dict[str, Any]) -> str:
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
    lines = ["Recovery trigger (what sent the task into recovery):"]
    for key, value in recovery_trigger.items():
        lines.append(f"- {key}: {value}")
    explanation = prompt.get("recovery_failure_explanation")
    if explanation:
        lines.append(f"- recovery_failure_explanation: {explanation}")
    return "\n".join(lines)


def _recovery_execution_root_section(prompt: dict[str, Any]) -> str:
    lines = ["Recovery execution repo (this recovery turn should patch Litehive here):"]
    lines.append(f"- recovery_execution_root: {prompt.get('recovery_execution_root')}")
    source_path = str(prompt.get("litehive_source_path") or "").strip()
    if source_path:
        lines.append(f"- configured_litehive_source_path: {source_path}")
    lines.append("- Recovery is for Litehive infrastructure bugs. Do not use the task worktree as the primary edit target.")
    return "\n".join(lines)


def _failed_subagent_diagnostics_section(diagnostics: dict[str, Any]) -> str:
    blocks: list[str] = []
    lines = [
        "Failed subagent diagnostics (read this before changing Litehive code):",
        f"- subagent_id: {diagnostics.get('subagent_id') or '-'}",
        f"- role: {diagnostics.get('role') or '-'}",
        f"- engine: {diagnostics.get('engine') or '-'}",
        f"- status: {diagnostics.get('status') or '-'}",
        f"- path: {diagnostics.get('path') or '-'}",
        f"- exit_code: {diagnostics.get('exit_code') if diagnostics.get('exit_code') is not None else '-'}",
        f"- did_produce_output: {'yes' if diagnostics.get('did_produce_output') else 'no'}",
    ]
    blocks.append("\n".join(lines))
    blocks.append(
        "\n".join(
            [
                "Diagnosis checklist:",
                "- Did the agent produce output?",
                "- Search the transcript/stdout/stderr for `litehive report`. Did it try to call it?",
                "- If it called `litehive report`, what exact Litehive error did it get?",
                "- What Litehive code path caused that failure, and what is the smallest safe fix?",
                "- Do not rerun the failed stage's task work or submit that stage's verdict yourself.",
            ]
        )
    )
    session_payload = diagnostics.get("session")
    if isinstance(session_payload, dict) and session_payload:
        blocks.append(_yaml_block("session.yaml (materialized from the subagent session store)", session_payload))
    report_payload = diagnostics.get("report")
    if isinstance(report_payload, dict) and report_payload:
        blocks.append(_yaml_block("report.yaml (materialized from the subagent report store)", report_payload))
    transcript = str(diagnostics.get("transcript") or "")
    if transcript:
        blocks.append(
            _text_block(
                "transcript.md",
                transcript,
                limit=_RECOVERY_ARTIFACT_HEAD_CHARS,
                tail=False,
            )
        )
    stdout = str(diagnostics.get("stdout") or "")
    if stdout:
        blocks.append(
            _text_block(
                "stdout.txt",
                stdout,
                limit=_RECOVERY_ARTIFACT_TAIL_CHARS,
                tail=True,
            )
        )
    stderr = str(diagnostics.get("stderr") or "")
    if stderr:
        blocks.append(
            _text_block(
                "stderr.txt",
                stderr,
                limit=_RECOVERY_ARTIFACT_TAIL_CHARS,
                tail=True,
            )
        )
    return "\n\n".join(blocks)


def _recovery_history_section(recovery_history: list[dict[str, Any]]) -> str:
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
    """Build scope analysis section for recovery agent prompts."""
    lines = ["Scope analysis (operator cleanup vs SWE scope creep):"]

    is_operator_cleanup = scope_analysis.get("is_operator_cleanup", False)
    classification = "OPERATOR CLEANUP" if is_operator_cleanup else "POTENTIAL SCOPE CREEP"
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
    bullets = "\n".join(f"- {f}" for f in conflict_files)
    extra = f"\nMerge attempt: {merge_attempt}" if merge_attempt is not None else ""
    return f"Merge conflict files (resolve all of these):\n{bullets}{extra}"


def _nudge_section(prompt: dict[str, Any]) -> str:
    message = str(prompt.get("nudge_message") or "").strip()
    lines = [
        "IMPORTANT: this is a nudge because your prior turn ended without a verdict submission.",
    ]
    if message:
        lines.append(message)
    lines.append(
        "Do not continue exploratory work until you have submitted `litehive report` with the correct verdict."
    )
    return "\n".join(lines)


_MESSAGE_CAP = 500
_TRUNCATION_MARKER = "…(truncated)"


def _cap_message(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy with message capped to _MESSAGE_CAP chars total."""
    msg = entry.get("message", "")
    if len(msg) <= _MESSAGE_CAP:
        return entry
    keep = max(_MESSAGE_CAP - len(_TRUNCATION_MARKER), 0)
    return {**entry, "message": msg[:keep] + _TRUNCATION_MARKER}


def _string_list(value: Any) -> list[str]:
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


def _compact_list(items: list[str], *, limit: int, separator: str = ", ") -> str:
    if len(items) <= limit:
        return separator.join(items)
    shown = separator.join(items[:limit])
    return f"{shown}{separator}+{len(items) - limit} more"


def _yaml_block(label: str, payload: dict[str, Any]) -> str:
    dumped = yaml.safe_dump(payload, sort_keys=False).rstrip()
    return f"{label}:\n```yaml\n{dumped}\n```"


def _text_block(label: str, text: str, *, limit: int, tail: bool) -> str:
    total = len(text)
    if total <= limit:
        preview = text
        direction = f"{total} chars"
    elif tail:
        preview = f"{_TRUNCATION_MARKER}\n{text[-limit:]}"
        direction = f"{total} chars, showing last {limit}"
    else:
        preview = f"{text[:limit]}\n{_TRUNCATION_MARKER}"
        direction = f"{total} chars, showing first {limit}"
    return f"{label} ({direction}):\n```text\n{preview}\n```"


def _pipeline_stage_key(name: str | None) -> str | None:
    if name in {"before_grooming", "grooming", "after_grooming"}:
        return "grooming"
    if name in {"before_implementing", "implementing", "after_implementing"}:
        return "implementing"
    if name in {"before_testing", "testing", "after_testing"}:
        return "testing"
    if name in {"before_accepting", "accepting", "after_accepting"}:
        return "accepting"
    if name in {"before_commit", "commit", "after_commit", "merge_resolving"}:
        return "commit_to_git"
    return name


def _entry_sources(entry: dict[str, Any]) -> set[str]:
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
    if entry.get("verdict") != "reject":
        return False

    rejection_source = str(last_rejection.get("source") or "").strip()
    if rejection_source and rejection_source not in _entry_sources(entry):
        return False

    rejection_stage = _pipeline_stage_key(str(last_rejection.get("raised_at_phase") or "").strip() or None)
    entry_stage = _pipeline_stage_key(str(entry.get("stage") or "").strip() or None)
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

    if current_stage == "grooming":
        pass  # no activity context needed

    elif current_stage == "implementing":
        g = _last_where(stage="grooming", verdict="pass")
        if g:
            kept.append(g)
        # On retry, the rejection is rendered in the dedicated last_rejection
        # section; do not repeat older reject entries in the activity section.
        if not last_rejection:
            for e in reversed(activity):
                if e.get("verdict") == "reject" and e.get("stage") in (
                    "testing",
                    "accepting",
                    "implementing",
                ):
                    kept.append(e)
                    break

    elif current_stage == "testing":
        p = _last_where(stage="implementing", verdict="pass")
        if p:
            kept.append(p)

    elif current_stage == "accepting":
        p = _last_where(stage="implementing", verdict="pass")
        if p:
            kept.append(p)
        t = _last_where(stage="testing", verdict="pass")
        if t:
            kept.append(t)

    elif current_stage == "recovering":
        p = _last_where(stage="implementing", verdict="pass")
        if p:
            kept.append(p)
        # The crash or rejection that triggered recovery
        for e in reversed(activity):
            if e.get("verdict") in ("reject", "blocked"):
                kept.append(e)
                break

    else:
        # Fallback: grooming pass + last per (stage, verdict)
        g = _last_where(stage="grooming", verdict="pass")
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
    trimmed = _trim_activity_for_prompt(activity, current_stage, last_rejection)
    if not trimmed:
        return ""
    blocks: list[str] = []
    for entry in trimmed:
        role = entry.get("role", "?")
        stage = entry.get("stage", "?")
        verdict = entry.get("verdict", "comment")
        classification = entry.get("verdict_classification") or entry.get("classification")
        verdict_label = f"{verdict}; classification={classification}" if classification else str(verdict)
        message = entry.get("message", "")
        blocks.append(f"[{stage}] {role} ({verdict_label}): {message}")
    return "Task activity:\n" + "\n".join(blocks)


def _runner_hooks_section(stage: str | None, hooks: list[dict[str, Any]]) -> str:
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
    verdicts = "<resume|advance|done|budget_hit|reject>" if prompt.get("role") == "recovery" else "<pass|reject>"
    example_verdict = "resume" if prompt.get("role") == "recovery" else "pass"
    role = prompt.get("role") or "swe"
    return (
        "IMPORTANT: when you are done, submit your verdict by running:\n"
        f'  litehive report --verdict {example_verdict} --role {role} --message "your report text"\n'
        f"Allowed verdicts for your role: {verdicts}.\n\n"
        "If the message is multiline or contains shell-sensitive characters, write it to /tmp/verdict_msg.txt and pass --message-file /tmp/verdict_msg.txt instead.\n"
        "Your message is the primary signal the next agent receives — write it as if it's the only thing they read.\n"
        "On reject: include EXPECTED behavior, OBSERVED behavior, reproduction steps, and which acceptance criteria are not met."
    )


def _label_to_heading(label: str) -> str:
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
    if not stage:
        return "this stage"
    return stage.replace("_", " ")
