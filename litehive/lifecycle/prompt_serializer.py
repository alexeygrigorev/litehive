"""Serialize a v2 ``RoleAgent.build_prompt()`` dict into a string for engines.

The v2 ``RoleAgent`` returns a structured dict so the test layer can inspect
it and the engine adapter layer can format it however its CLI expects.
``serialize_prompt()`` produces the canonical text representation: a
section-based document an engine adapter can pipe to ``codex run`` or
``claude run`` etc.

The serializer:
  - reads the v1 ``TaskRecord`` for goal / acceptance / plan / constraints
    (the dict only carries task_id; the rest comes from task.yaml)
  - composes the four instruction layers from the dict
  - surfaces ``last_rejection`` so the next agent visit can act on it
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
    discussion thread so the next agent visit sees previous stage
    verdicts.
    """
    if task_record is None and workspace_root is not None:
        task_record = get_task(workspace_root, prompt["task_id"])

    thread = prompt.get("thread") or []
    if not thread and workspace_root is not None and task_record is not None:
        thread = _load_task_activity_history(workspace_root, task_record)

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

    recovery_trigger = prompt.get("recovery_trigger")
    if recovery_trigger:
        sections.append(_recovery_trigger_section(recovery_trigger, prompt))

    conflict_files = prompt.get("conflict_files")
    if conflict_files:
        sections.append(_merge_conflict_section(conflict_files, prompt.get("merge_attempt")))

    if prompt.get("nudge"):
        sections.append(_nudge_section(prompt))

    if thread:
        sections.append(
            _thread_section(
                thread,
                current_stage=prompt.get("stage"),
                last_rejection=last_rejection,
            )
        )

    rejecting_hooks = prompt.get("rejecting_hooks") or []
    if rejecting_hooks:
        sections.append(_rejecting_hooks_section(rejecting_hooks))

    sections.append(_verdict_instructions_section(prompt))
    return (SECTION_SEP * 2).join(s for s in sections if s).strip() + "\n"


def _load_task_activity_history(workspace_root: Path, task_record: TaskRecord) -> list[dict[str, Any]]:
    """Read the task's persisted activity entries."""
    try:
        comments = load_task_activity(workspace_root, task_record)
    except (OSError, ValidationError, yaml.YAMLError):
        return []
    return [
        {
            "role": c.role,
            "stage": c.stage,
            "verdict": c.verdict,
            "message": c.message,
        }
        for c in comments
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
    return (
        "Last rejection (from the previous attempt at this stage):\n"
        f"- Source: {rejection.get('source')}\n"
        f"- Raised at phase: {rejection.get('raised_at_phase')}\n"
        f"- Reason: {rejection.get('reason')}\n"
        "Address this concretely in your work this turn."
    )


def _recovery_trigger_section(recovery_trigger: dict[str, Any], prompt: dict[str, Any]) -> str:
    lines = ["Recovery trigger (what sent the task into recovery):"]
    for key, value in recovery_trigger.items():
        lines.append(f"- {key}: {value}")
    explanation = prompt.get("recovery_failure_explanation")
    if explanation:
        lines.append(f"- recovery_failure_explanation: {explanation}")
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
        "Do not continue exploratory work until you have submitted `litehive agent report` with the correct verdict."
    )
    return "\n".join(lines)


_MESSAGE_CAP = 500


def _cap_message(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy with message truncated to _MESSAGE_CAP chars."""
    msg = entry.get("message", "")
    if len(msg) <= _MESSAGE_CAP:
        return entry
    return {**entry, "message": msg[:_MESSAGE_CAP] + "…(truncated)"}


def _trim_thread_for_prompt(
    thread: list[dict[str, Any]],
    current_stage: str | None,
    last_rejection: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Keep only the thread entries the current stage actually needs.

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
    # Filter out recovery bookkeeping comments
    thread = [e for e in thread if not (e.get("role") == "recovery" and e.get("verdict") == "comment")]

    if not thread:
        return []

    # Skip entries that duplicate last_rejection
    if last_rejection:
        rej_reason = last_rejection.get("reason", "")
        rej_source = last_rejection.get("source", "")
        thread = [
            e
            for e in thread
            if not (
                e.get("verdict") == "reject"
                and (e.get("source") or e.get("role", "")) == rej_source
                and e.get("message", "") == rej_reason
            )
        ]

    def _last_where(**match: str) -> dict[str, Any] | None:
        for e in reversed(thread):
            if all(e.get(k) == v for k, v in match.items()):
                return e
        return None

    kept: list[dict[str, Any]] = []

    if current_stage == "grooming":
        pass  # no thread context needed

    elif current_stage == "implementing":
        g = _last_where(stage="grooming", verdict="pass")
        if g:
            kept.append(g)
        # On retry, the rejection is rendered in the dedicated last_rejection
        # section; do not repeat older reject entries in the thread.
        if not last_rejection:
            for e in reversed(thread):
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
        for e in reversed(thread):
            if e.get("verdict") in ("reject", "blocked"):
                kept.append(e)
                break

    else:
        # Fallback: grooming pass + last per (stage, verdict)
        g = _last_where(stage="grooming", verdict="pass")
        if g:
            kept.append(g)
        seen: set[tuple[str, str]] = set()
        for e in reversed(thread):
            key = (e.get("stage", "?"), e.get("verdict", "?"))
            if key not in seen:
                seen.add(key)
                kept.append(e)

    # Deduplicate, preserve original order, cap messages
    kept_ids = {id(e) for e in kept}
    return [_cap_message(e) for e in thread if id(e) in kept_ids]


def _thread_section(
    thread: list[dict[str, Any]],
    current_stage: str | None = None,
    last_rejection: dict[str, Any] | None = None,
) -> str:
    trimmed = _trim_thread_for_prompt(thread, current_stage, last_rejection)
    blocks: list[str] = []
    for entry in trimmed:
        role = entry.get("role", "?")
        stage = entry.get("stage", "?")
        verdict = entry.get("verdict", "comment")
        message = entry.get("message", "")
        blocks.append(f"[{stage}] {role} ({verdict}): {message}")
    return "Discussion thread:\n" + "\n".join(blocks)


def _rejecting_hooks_section(hooks: list[dict[str, Any]]) -> str:
    lines = ["Checks that will reject your work if they fail:"]
    for hook in hooks:
        cmd = hook.get("command", "")
        desc = hook.get("description", "")
        if desc:
            lines.append(f"- {cmd} ({desc})")
        else:
            lines.append(f"- {cmd}")
    lines.append(
        "Run these checks yourself before submitting your verdict. If they fail, the after-stage hook will reject your work and you will need to fix it."
    )
    return "\n".join(lines)


def _verdict_instructions_section(prompt: dict[str, Any]) -> str:
    verdicts = (
        "<resume|advance|done|budget_hit|reject>" if prompt.get("role") == "recovery" else "<pass|reject|blocked>"
    )
    return (
        "IMPORTANT: when you are done, submit your verdict by running:\n"
        "  echo 'your report text' > /tmp/verdict_msg.txt\n"
        f"  litehive agent report --verdict {verdicts} --message-file /tmp/verdict_msg.txt\n\n"
        "Always use --message-file to avoid shell quoting issues with backticks or special characters.\n"
        "Your message is the primary signal the next agent receives — write it as if it's the only thing they read.\n"
        "On reject: include EXPECTED behavior, OBSERVED behavior, reproduction steps, and which acceptance criteria are not met."
    )


def _label_to_heading(label: str) -> str:
    mapping = {
        "role": "Role guidance",
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
