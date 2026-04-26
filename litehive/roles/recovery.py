from pathlib import Path
from typing import Any

from litehive.agents.execution_trace import load_subagent_execution_trace
from litehive.agents.session_store import load_subagent_report, load_subagent_session
from litehive.config.loading import load_config
from litehive.domain.common import PipelineState
from litehive.domain.runtime import RuntimeRecoveryOutcome
from litehive.lifecycle.events import Event, RecoveryBudgetHit, RecoveryFailed, RecoverySucceeded
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.persistence import TaskState
from litehive.recovery.scope_analysis import analyze_scope_changes
from litehive.state.records import get_task_record
from litehive.tasks.paths import latest_subagent_base, read_text_artifact, resolve_artifact_path, task_dir

from .base import RoleAgent

ROLE_GUIDANCE = """\
- You are the recovery agent responsible for diagnosing why this task stopped making progress and restoring a runnable path.
- Your job is to diagnose why the previous agent failed and restore a runnable path by fixing Litehive infrastructure bugs.
- You fix Litehive infrastructure bugs, not agent judgment disagreements. Semantic QA/reviewer rejects are not your job.
- Pull logs before diagnosing. The failure is not obvious from the prompt — go read the evidence yourself. Sources, in order of value:
  - `litehive pipeline journal <task_id>` — start here. One command, no sqlite incantations: dumps the task state (stage, active recovery trigger, recovery history, failed reason, last rejection by stage), the lifecycle events, and the recent pipeline transitions in one readable block.
  - `litehive task logs <task_id> --agent` — execution trace / stdout / stderr of the failing subagent process. This is usually where the root cause is.
  - `litehive task logs <task_id> --agent --all` — lists every subagent run on this task so you can diff the recent ones.
  - `litehive task logs <task_id>` — task journal (v1 style) with stage entries, verdict submissions, and operator notes.
  - `litehive task logs --daemon` — daemon-level events if you suspect an orchestrator/runner bug rather than an agent bug.
  - `litehive pipeline rules` — the full v2 transition table, if you need to understand what routing decisions the state machine made.
  - `litehive task debug <task_id>` — latest stage report context, verdict history, and operator activity.
  - The `recovery_trigger` field in your prompt already contains the most recent trigger event, source, and reason — use it to narrow your log search.
  - If you need to go deeper than the CLI commands, the underlying tables are `stage_reports`, `recovery_reports`, `pipeline_transitions` (columns: `seq, created_at, from_stage, event_type, event_payload, to_stage, rule_description, delta`) and `pipeline_journal` (columns: `seq, created_at, kind, payload`). Don't invent column names.
- Diagnose the failing agent before you touch code:
  - Did the agent produce any stdout, stderr, or execution-trace output?
  - Did it try to call `litehive report`?
  - If it tried, what exact Litehive error did it get?
  - What Litehive code path caused that failure, and what is the smallest safe fix?
- Your job is not to redo the failed stage's work, not to re-run the task's implementation or verification, and not to submit the failed stage verdict on the previous agent's behalf.
- Make the smallest effective fix needed so the task can resume the current stage and finish cleanly.
- If this workspace is not already the Litehive repo, switch into the repo at `litehive_source_path` and repair Litehive there.
- Work in the Litehive source repo so you can fix the orchestrator, adapters, prompts, report wiring, resume logic, or other infrastructure bugs with the smallest safe change.
- Example: if the failed agent tried `litehive report` and got a Litehive traceback, fix Litehive's report path or resume wiring here, verify the Litehive fix, then submit a recovery verdict.
- Non-example: do not rerun the failed stage's tests, do not finish the task's feature work, and do not submit a verdict on the failed agent's behalf.
- run `uv run pytest` in the Litehive repo before reporting success when you changed Litehive code; keep verification targeted.
- If the evidence points to a project/task bug rather than a Litehive bug, do not implement the task; report that no Litehive infrastructure fix was found and leave the task for the normal stage owner.
- Submit your own recovery verdict describing the root cause, the Litehive fix you made, and why the failed stage should be retried.
- If you submit `resume` or `advance`, include a concrete `--target-stage <stage>`; do not leave the destination implicit.
- If the prompt shows a repeated recovery fingerprint for the same origin stage, do not `resume` or `advance` again.
- On a repeated recovery fingerprint, create a follow-up bug task for the unfixable failure, then submit `litehive report --verdict reject --follow-up-task <task-id> --message "<fingerprint + follow-up reference>"` so Litehive flags the current task with the reference instead of re-routing it.
"""

FRESH_ATTEMPT_GUIDANCE = """\
- Fresh recovery attempt: gather the failing Litehive evidence first, then make the smallest infrastructure fix that restores a runnable path.
"""

RETRY_ATTEMPT_GUIDANCE = """\
- Retry after rejection: read the last rejection carefully before you diagnose or patch Litehive.
- Rerun the cited reproduction or verification commands exactly when they are still applicable to the current failure.
- Fix the cited Litehive infrastructure failures, or prove with current evidence that the issue belongs to task code rather than Litehive.
- Do not escape through `blocked`, stale, or environmental claims without current evidence from this worktree or the Litehive source repo.
"""


class RecoveryAgent(RoleAgent):
    """Singleton recovery node, reachable from any stage.

    Reads the active structured recovery trigger from ``TaskState`` — no
    per-entry construction, no ``RecoveryRequest`` object, and no separate
    recovery context payload. The ``RecoveryTrigger`` contains all recovery
    context. Fits into the ``NodeRegistry`` like every other node.

    Verdict mapping differs from a regular stage agent: recovery emits
    ``RecoverySucceeded`` / ``RecoveryFailed`` / ``RecoveryBudgetHit`` instead
    of ``Pass`` / ``Reject``.
    """

    NODE_NAME = PipelineState.RECOVERING
    ROLE = "recovery"
    ROLE_INSTRUCTIONS = ROLE_GUIDANCE
    FRESH_ATTEMPT_INSTRUCTIONS = FRESH_ATTEMPT_GUIDANCE
    RETRY_ATTEMPT_INSTRUCTIONS = RETRY_ATTEMPT_GUIDANCE

    def build_prompt(self, state: TaskState) -> dict[str, Any]:
        base = super().build_prompt(state)
        trigger = state.active_recovery_trigger
        task_record = None
        recovery_history: list[dict[str, Any]] = []
        repeated_recovery_fingerprint = None
        recovery_execution_root = None
        litehive_source_path = None
        failed_subagent_diagnostics = None
        scope_analysis = None

        if task_record is None and self.prompt_context and self.prompt_context.workspace_root:
            task_record = get_task_record(self.prompt_context.workspace_root, state.task_id)
        root = None if self.prompt_context is None else self.prompt_context.workspace_root
        litehive_source_path, recovery_execution_root = _recovery_source_checkout(root)
        failed_subagent_diagnostics = _failed_subagent_diagnostics_payload(root, task_record)
        recovery_history = _merged_recovery_history_payload(state, task_record)
        repeated_recovery_fingerprint = _repeated_recovery_fingerprint_payload(trigger, recovery_history)

        # Analyze scope changes to distinguish operator cleanup from SWE scope creep
        if root is not None:
            scope_analysis = analyze_scope_changes(root, state.task_id)

        base.update(
            {
                "litehive_source_path": litehive_source_path,
                "recovery_execution_root": recovery_execution_root,
                "recovery_trigger": trigger.to_payload() if trigger is not None else None,
                "recovery_history": recovery_history,
                "repeated_recovery_fingerprint": repeated_recovery_fingerprint,
                "recovery_failure_explanation": state.recovery_failure_explanation,
                "failed_subagent_diagnostics": failed_subagent_diagnostics,
                "scope_analysis": scope_analysis,
            }
        )
        return base

    def verdict_to_event(self, verdict: AgentVerdict) -> Event:
        outcome = verdict.outcome.lower()
        if outcome == "resume":
            target = str(verdict.metadata.get("target_stage") or "").strip()
            if not target:
                return RecoveryFailed(reason="recovery resume verdict missing target_stage")
            return RecoverySucceeded(resume=target, disposition_hint="resume")
        if outcome == "advance":
            target = str(verdict.metadata.get("target_stage") or "").strip()
            if not target:
                return RecoveryFailed(reason="recovery advance verdict missing target_stage")
            return RecoverySucceeded(resume=target, disposition_hint="advance")
        if outcome == "done":
            return RecoverySucceeded(resume="done", disposition_hint="done")
        if outcome == "budget_hit":
            return RecoveryBudgetHit()
        return RecoveryFailed(reason=verdict.reason or "recovery_failed")


def _pipeline_stage_key(name: str | None) -> str | None:
    if name in {"before_grooming", "grooming", "after_grooming", "recovering"}:
        return "grooming"
    if name in {"before_implementing", "implementing", "after_implementing"}:
        return "implementing"
    if name in {"before_testing", "testing", "after_testing"}:
        return "testing"
    if name in {"before_accepting", "accepting", "after_accepting"}:
        return "accepting"
    if name in {"commit", "after_commit", "merge_resolving"}:
        return "commit_to_git"
    return name


def _recovery_history_key(item: dict[str, Any]) -> tuple[str | None, str, str, str, str | None]:
    return (
        item.get("origin_stage"),
        str(item.get("fingerprint") or ""),
        str(item.get("budget_key") or ""),
        str(item.get("recovery_verdict") or ""),
        item.get("created_at"),
    )


def _runtime_recovery_payload(outcome: RuntimeRecoveryOutcome) -> dict[str, Any]:
    return outcome.model_dump(mode="json")


def _state_recovery_payload(outcome: Any) -> dict[str, Any]:
    trigger = outcome.trigger
    return {
        "origin_stage": trigger.origin_stage,
        "trigger_event_kind": trigger.trigger_event_kind.value,
        "fingerprint": trigger.failure_fingerprint.fingerprint,
        "classification": trigger.failure_fingerprint.classification,
        "budget_key": trigger.budget_key(),
        "recovery_verdict": outcome.recovery_verdict,
        "disposition": outcome.disposition.value,
        "reason_code": outcome.reason_code,
        "message": outcome.message,
        "created_at": outcome.created_at,
    }


def _merged_recovery_history_payload(state: TaskState, task_record: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str, str, str, str | None]] = set()
    runtime_history = [] if task_record is None else list(task_record.runtime.pipeline.recovery_history)
    items = [
        *[_runtime_recovery_payload(outcome) for outcome in runtime_history],
        *[_state_recovery_payload(outcome) for outcome in state.recovery_history],
    ]
    for item in items:
        key = _recovery_history_key(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _same_recovery_path(current_trigger: Any, prior: dict[str, Any]) -> bool:
    current_origin = _pipeline_stage_key(current_trigger.origin_stage)
    prior_origin = _pipeline_stage_key(prior.get("origin_stage"))
    current_fingerprint = str(current_trigger.failure_fingerprint.fingerprint or "")
    prior_fingerprint = str(prior.get("fingerprint") or "")
    if current_origin != prior_origin:
        return False
    if not current_fingerprint or not prior_fingerprint:
        return False
    return current_fingerprint == prior_fingerprint


def _repeated_recovery_fingerprint_payload(
    trigger: Any,
    recovery_history: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if trigger is None:
        return None
    matches = [
        item
        for item in recovery_history
        if item.get("recovery_verdict") != "budget_hit" and _same_recovery_path(trigger, item)
    ]
    if not matches:
        return None
    return {
        "count": len(matches) + 1,
        "origin_stage": trigger.origin_stage,
        "fingerprint": trigger.failure_fingerprint.fingerprint,
        "classification": trigger.failure_fingerprint.classification,
        "budget_key": trigger.budget_key(),
        "matching_prior_attempts": matches,
    }


def _recovery_source_checkout(root: Path | None) -> tuple[str | None, str | None]:
    if root is None:
        return None, None
    try:
        config = load_config(root)
    except Exception:
        return None, str(root)
    raw_source = str(config.litehive_source_path or "").strip() or None
    if raw_source is None:
        return None, str(root)
    candidate = Path(raw_source).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    execution_root = resolved if resolved.is_dir() else root
    return raw_source, str(execution_root)


def _failed_subagent_diagnostics_payload(root: Path | None, task_record: Any) -> dict[str, Any] | None:
    if root is None or task_record is None:
        return None
    subagent_base = latest_subagent_base(root, task_record)
    if subagent_base is None or not subagent_base.exists():
        return None

    rel_path = str(subagent_base.relative_to(task_dir(root, task_record)))
    runtime_state = None
    interruption = task_record.runtime.execution.interruption
    interrupted_subagent = None if interruption is None else interruption.subagent
    for candidate in (task_record.runtime.execution.active_subagent, interrupted_subagent):
        if candidate is not None and (candidate.path == rel_path or subagent_base.name.startswith(candidate.id)):
            runtime_state = candidate
            break
    subagent_ref = next(
        (
            ref
            for ref in reversed(task_record.subagents)
            if ref.path == rel_path or (runtime_state is not None and ref.id == runtime_state.id)
        ),
        None,
    )
    subagent_id = (
        runtime_state.id
        if runtime_state is not None
        else subagent_ref.id
        if subagent_ref is not None
        else subagent_base.name.split("-", 2)[0]
    )
    if not subagent_id:
        return None

    session_payload = load_subagent_session(root, task_record.id, subagent_id)
    report_payload = load_subagent_report(root, task_record.id, subagent_id)
    trace_ref = runtime_state or subagent_ref
    execution_trace_view = (
        None
        if trace_ref is None
        else load_subagent_execution_trace(
            root,
            task_record,
            trace_ref,
            active=runtime_state is not None and runtime_state.status == "running",
            runtime_state=runtime_state,
        )
    )
    execution_trace = "" if execution_trace_view is None else execution_trace_view.text
    stdout = _read_subagent_artifact(subagent_base, "stdout.txt")
    stderr = _read_subagent_artifact(subagent_base, "stderr.txt")
    exit_code = None
    if runtime_state is not None:
        exit_code = runtime_state.exit_code
    if exit_code is None:
        session_exit_code = session_payload.get("exit_code")
        if isinstance(session_exit_code, int):
            exit_code = session_exit_code

    return {
        "subagent_id": subagent_id,
        "role": (
            runtime_state.role if runtime_state is not None else subagent_ref.role if subagent_ref is not None else None
        ),
        "engine": (
            runtime_state.engine
            if runtime_state is not None
            else subagent_ref.engine
            if subagent_ref is not None
            else None
        ),
        "status": (
            runtime_state.status
            if runtime_state is not None
            else subagent_ref.status
            if subagent_ref is not None
            else None
        ),
        "path": rel_path,
        "exit_code": exit_code,
        "did_produce_output": any(text.strip() for text in (execution_trace, stdout, stderr)),
        "session": session_payload,
        "report": report_payload,
        "transcript": execution_trace,
        "stdout": stdout,
        "stderr": stderr,
    }


def _read_subagent_artifact(subagent_base: Path, artifact_name: str) -> str:
    artifact_path = resolve_artifact_path(subagent_base, artifact_name)
    if artifact_path is None:
        return ""
    try:
        return read_text_artifact(artifact_path)
    except Exception:
        return ""
