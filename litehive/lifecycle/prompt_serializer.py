"""Serialize an :class:`AgentPrompt` into a string for engines.

``RoleAgent`` returns a typed :class:`AgentPrompt` (or
:class:`RecoveryPrompt` for the recovery role) so the test layer can
inspect it and the engine adapter layer can format it however its CLI
expects. ``serialize_prompt()`` produces the canonical text
representation: a section-based document an engine adapter can pipe to
``codex run`` or ``claude run`` etc.

The serializer:
  - reads the caller-resolved ``TaskRecord`` for goal / acceptance / plan / constraints
    (the prompt no longer carries task_id; the agent never sees internal IDs)
  - composes the selected instruction layers from the prompt
  - surfaces ``last_rejection`` as context for retry prompts
  - surfaces ``recovery_trigger`` for recovery agents
  - finishes with the ``litehive agent report`` verdict instructions

Section builders live in :mod:`litehive.lifecycle.prompt_sections`; the
activity history rendering stays here because it's tightly coupled to the
``load_task_activity`` data path tests monkey-patch on this module.
"""

from pathlib import Path
from typing import Any

from pydantic import ValidationError
import yaml

from litehive.domain.common import PipelineState, TaskStage, pipeline_stage_key
from litehive.domain.task import TaskRecord
from litehive.lifecycle.prompt_sections import (
    _acceptance_criteria_section,
    _constraints_section,
    _failed_run_history_section,
    _failed_subagent_diagnostics_section,
    _goal_section,
    _header_section,
    _instructions_section,
    _last_rejection_section,
    _merge_conflict_section,
    _nudge_section,
    _plan_section,
    _prior_work_section,
    _recovery_execution_root_section,
    _recovery_history_section,
    _recovery_trigger_section,
    _repeated_recovery_fingerprint_section,
    _runner_hooks_section,
    _scope_analysis_section,
    _test_failure_attribution_section,
    _verdict_instructions_section,
)
from litehive.lifecycle.prompt_types import AgentPrompt, RecoveryPrompt
from litehive.tasks.activity import load_task_activity
from litehive.workspace import Workspace


SECTION_SEP = "\n"


def serialize_prompt(
    prompt: AgentPrompt,
    *,
    task_record: TaskRecord | None,
    workspace_root: Path,
) -> str:
    """Render the typed prompt + task record into the engine-facing string.

    ``task_record`` is a required keyword argument resolved by the
    caller (engine adapter) so the serializer never has to look it up
    itself; it may still be ``None`` for callers that legitimately have
    no task (tests, the no-record fallback path), in which case the
    goal/acceptance/plan/constraints sections render placeholders.
    ``workspace_root`` is the repo root and is used to load the task's
    activity history so the next agent visit sees previous stage
    verdicts.
    """
    activity = prompt.activity or []
    if not activity and task_record is not None:
        activity = _load_task_activity_history(Workspace.from_path(workspace_root), task_record)

    sections: list[str] = []
    sections.append(_header_section(prompt, task_record))
    sections.append(_instructions_section(prompt))
    sections.append(_goal_section(task_record))
    sections.append(_acceptance_criteria_section(task_record))
    sections.append(_plan_section(task_record))
    sections.append(_constraints_section(task_record))

    last_rejection = prompt.last_rejection
    if last_rejection:
        sections.append(_last_rejection_section(last_rejection))

    if (prompt.stage_retry or 0) > 0:
        prior_work = _prior_work_section(prompt.last_report or {}, last_rejection)
        if prior_work:
            sections.append(prior_work)

    if isinstance(prompt, RecoveryPrompt):
        if prompt.recovery_trigger:
            sections.append(_recovery_trigger_section(prompt.recovery_trigger, prompt))

        if prompt.recovery_execution_root:
            sections.append(_recovery_execution_root_section(prompt))

        if prompt.failed_subagent_diagnostics:
            sections.append(_failed_subagent_diagnostics_section(prompt.failed_subagent_diagnostics))

        if prompt.recovery_history:
            sections.append(_recovery_history_section(prompt.recovery_history))

    if prompt.failed_run_history:
        sections.append(_failed_run_history_section(prompt.failed_run_history))

    if isinstance(prompt, RecoveryPrompt):
        if prompt.repeated_recovery_fingerprint:
            sections.append(_repeated_recovery_fingerprint_section(prompt.repeated_recovery_fingerprint))

        if prompt.scope_analysis:
            sections.append(_scope_analysis_section(prompt.scope_analysis))

        if prompt.test_failure_attribution:
            sections.append(_test_failure_attribution_section(prompt.test_failure_attribution))

    if prompt.conflict_files:
        sections.append(_merge_conflict_section(prompt.conflict_files, prompt.merge_attempt))

    if prompt.nudge:
        sections.append(_nudge_section(prompt))

    if activity:
        sections.append(
            _activity_section(
                activity,
                current_stage=prompt.stage,
                last_rejection=last_rejection,
            )
        )

    runner_hooks = prompt.runner_hooks or []
    if runner_hooks:
        sections.append(_runner_hooks_section(prompt.stage, runner_hooks))

    sections.append(_verdict_instructions_section(prompt))
    return (SECTION_SEP * 2).join(s for s in sections if s).strip() + "\n"


def _load_task_activity_history(workspace: Workspace, task_record: TaskRecord) -> list[dict[str, Any]]:
    """
    Read the task's persisted activity entries for the prompt.

    Called only when the typed prompt did not pre-populate ``activity``
    — typically the production path, where the serializer has to fall
    back to disk. Tests monkey-patch ``load_task_activity`` on this
    module which is why the history loader lives here rather than in
    ``prompt_sections``.
    """
    try:
        activity_entries = load_task_activity(workspace, task_record)
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


# ── activity history rendering ──────────────────────────────────────────
#
# Lives here (not in prompt_sections) because it's tightly coupled to the
# ``load_task_activity`` data path above and to ``last_rejection`` matching;
# tests also monkey-patch ``load_task_activity`` on this module.


_MESSAGE_CAP = 500
_TRUNCATION_MARKER = "…(truncated)"


def _cap_message(entry: dict[str, Any]) -> dict[str, Any]:
    """
    Cap an activity entry's message so one verbose verdict can't blow
    up the prompt size.

    Returns a shallow copy with the message truncated; callers keep
    the original entry intact so the persisted activity log still
    holds the full text even though the prompt sees a trimmed copy.
    """
    msg = entry.get("message", "")
    if len(msg) <= _MESSAGE_CAP:
        return entry
    keep = max(_MESSAGE_CAP - len(_TRUNCATION_MARKER), 0)
    return {**entry, "message": msg[:keep] + _TRUNCATION_MARKER}


def _entry_sources(entry: dict[str, Any]) -> set[str]:
    """
    Expand an activity entry's role into the set of source labels
    ``last_rejection`` might use to point at it.

    The activity log records role (``grooming``, ``hook``,
    ``recovery``, …) while ``last_rejection`` records a ``source``
    field that may be either the role or the generic
    ``agent``/``hook``. This bridges the two so dedupe matching does
    not miss when the labels disagree purely on level of generality.
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
    """
    Detect activity entries already rendered in the last-rejection section.

    Used by the prompt trimmer to avoid double-rendering: the
    dedicated last-rejection block carries the full reject text, so
    seeing the same entry again under "Task activity" would just push
    real context out of the prompt window for no benefit.
    """
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
        """Local closure: scan ``activity`` newest-first for the first entry that equals every kwarg.

        Used by the per-stage selection branches of ``_select_activity_entries``
        so each "give me the latest grooming pass" / "give me the latest QA
        reject" lookup reads as one line in the surrounding flow.
        """
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
    """
    Render the trimmed cross-stage activity log.

    Walked through ``_trim_activity_for_prompt`` first so the current
    agent only sees the prior verdicts that matter to its stage; the
    output is what the engine reads under the "Task activity" heading
    before deciding its next move.
    """
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


