from typing import Any
from pathlib import Path

from litehive.domain.recovery import TriggerEventKind, blocked_on_follow_up_reason
from litehive.domain.reports import RecoveryAction
from litehive.lifecycle.events import Crash, Event, RecoveryBudgetHit, RecoveryFailed, RecoverySucceeded
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.persistence import TaskState
from litehive.recovery.scope_analysis import analyze_scope_changes
from litehive.recovery.test_failure_attribution import (
    UNRELATED_TEST_BREAKAGE,
    TestFailureAttribution,
    attribute_test_failure,
    build_unrelated_test_follow_up,
)
from litehive.state.records import get_task_record
from litehive.state.records import create_follow_up_tasks
from litehive.tasks.reports import record_recovery_report
from litehive.tasks.worktrees import task_worktree_path
from .base import RoleAgent

INSTRUCTIONS = """\
- You are the recovery agent responsible for diagnosing why this task stopped making progress and restoring a runnable path.
- Your job is to diagnose why the previous agent failed and restore a runnable path by fixing Litehive infrastructure bugs.
- When evaluating worktree changes, distinguish between legitimate operator cleanup and SWE scope creep:
  - **Operator cleanup** (LEGITIMATE): Deleting files that were already broken, dead, or unused on main branch. This is expected SWE behavior per role instructions.
  - **SWE scope creep** (ILLEGITIMATE): Deleting healthy, functional tests or code to avoid fixing them instead of properly implementing the task.
  - Check the "Scope analysis" section in your prompt for automated classification. If classified as "OPERATOR CLEANUP", do not reject for scope creep.
  - Only flag scope creep if the analysis shows "POTENTIAL SCOPE CREEP" with healthy files deleted on main.
- Pull logs before diagnosing. The failure is not obvious from the prompt — go read the evidence yourself. Sources, in order of value:
  - `litehive pipeline journal <task_id>` — start here. One command, no sqlite incantations: dumps the task state (stage, active recovery trigger, recovery history, failed reason, last rejection by stage), the lifecycle events, and the recent pipeline transitions in one readable block.
  - `litehive task logs <task_id> --agent` — transcript / stdout / stderr of the failing subagent process. This is usually where the root cause is.
  - `litehive task logs <task_id> --agent --all` — lists every subagent run on this task so you can diff the recent ones.
  - `litehive task logs <task_id>` — task journal (v1 style) with stage entries, verdict submissions, and operator notes.
  - `litehive task logs --daemon` — daemon-level events if you suspect an orchestrator/runner bug rather than an agent bug.
  - `litehive pipeline rules` — the full v2 transition table, if you need to understand what routing decisions the state machine made.
  - `.litehive/tasks/<task_id>/reports/*.yaml` — stage reports the agent wrote (if any).
  - Task activity from `litehive task logs <task_id>` / `litehive task debug <task_id>` — verdict history and operator discussion.
  - The `recovery_trigger` field in your prompt already contains the most recent trigger event, source, and reason — use it to narrow your log search.
  - If you need to go deeper than the CLI commands, the underlying tables are `pipeline_transitions` (columns: `seq, created_at, from_stage, event_type, event_payload, to_stage, rule_description, delta`) and `pipeline_journal` (columns: `seq, created_at, kind, payload`). Don't invent column names.
- Your job is not to redo the failed stage's work, not to re-run the task's implementation or verification, and not to submit the failed stage verdict on the previous agent's behalf.
- Make the smallest effective fix needed so the task can resume the current stage and finish cleanly.
- If this workspace is not already the Litehive repo, switch into the repo at `litehive_source_path` and repair Litehive there.
- Work in the Litehive source repo so you can fix the orchestrator, adapters, prompts, report wiring, resume logic, or other infrastructure bugs with the smallest safe change.
- run `uv run pytest` in the Litehive repo before reporting success when you changed Litehive code; keep verification targeted.
- If the evidence points to a project/task bug rather than a Litehive bug, do not implement the task; report that no Litehive infrastructure fix was found and leave the task for the normal stage owner.
- Submit your own recovery verdict describing the root cause, the Litehive fix you made, and why the failed stage should be retried.
- If you submit `resume` or `advance`, include a concrete `--target-stage <stage>`; do not leave the destination implicit.
"""


class RecoveryAgent(RoleAgent):
    """Singleton recovery node, reachable from any stage.

    Reads the active structured recovery trigger from ``TaskState`` — no
    per-entry construction and no ``RecoveryRequest`` object. Fits into the
    ``NodeRegistry`` like every other node.

    Verdict mapping differs from a regular stage agent: recovery emits
    ``RecoverySucceeded`` / ``RecoveryFailed`` / ``RecoveryBudgetHit`` instead
    of ``Pass`` / ``Reject``.
    """

    NODE_NAME = "recovering"
    ROLE = "recovery"
    INSTRUCTIONS = INSTRUCTIONS

    def run(self, state: TaskState) -> Event:
        try:
            auto_event = self._auto_handle_unrelated_test_failure(state)
        except Exception as exc:
            return Crash(exc_type=type(exc).__name__, message=str(exc))
        if auto_event is not None:
            return auto_event
        return super().run(state)

    def build_prompt(self, state: TaskState) -> dict[str, Any]:
        base = super().build_prompt(state)
        trigger = state.active_recovery_trigger
        test_failure_attribution = self._test_failure_attribution(state)

        # Perform scope analysis if worktree can be determined
        scope_analysis = None
        try:
            worktree_path = None

            # Try to use current working directory if it's a git worktree
            cwd = Path.cwd()
            if (cwd / ".git").exists():
                worktree_path = cwd
            elif self.prompt_context and self.prompt_context.workspace_root:
                # Fall back to looking up via task record
                task_record = get_task_record(self.prompt_context.workspace_root, state.task_id)
                if task_record:
                    computed_worktree_path = task_worktree_path(self.prompt_context.workspace_root, task_record)
                    if computed_worktree_path.exists():
                        worktree_path = computed_worktree_path

            if worktree_path:
                analysis_result = analyze_scope_changes(worktree_path)
                # Convert NamedTuple to dict for prompt serializer
                scope_analysis = {
                    "is_operator_cleanup": analysis_result.is_operator_cleanup,
                    "deleted_files": analysis_result.deleted_files,
                    "broken_on_main": analysis_result.broken_on_main,
                    "healthy_on_main": analysis_result.healthy_on_main,
                    "reasoning": analysis_result.reasoning,
                }
        except Exception:
            # If scope analysis fails, provide no analysis (recovery can proceed without it)
            scope_analysis = None

        base.update(
            {
                "recovery_trigger": trigger.to_payload() if trigger is not None else None,
                "recovery_failure_explanation": state.recovery_failure_explanation,
                "scope_analysis": scope_analysis,
                "test_failure_attribution": (
                    None if test_failure_attribution is None else test_failure_attribution.to_prompt_payload()
                ),
            }
        )
        return base

    def _verdict_to_event(self, verdict: AgentVerdict) -> Event:
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

    def _test_failure_attribution(self, state: TaskState) -> TestFailureAttribution | None:
        trigger = state.active_recovery_trigger
        if trigger is None or trigger.trigger_event_kind != TriggerEventKind.REJECT:
            return None
        return attribute_test_failure(
            changed_files=state.last_report.changed_files,
            rejection_message=trigger.message,
            diagnostics=trigger.diagnostics,
        )

    def _auto_handle_unrelated_test_failure(self, state: TaskState) -> Event | None:
        root = self.prompt_context.workspace_root
        if root is None:
            return None
        trigger = state.active_recovery_trigger
        if trigger is None or trigger.trigger_event_kind != TriggerEventKind.REJECT:
            return None
        origin_stage = _follow_up_stage(trigger.origin_stage)
        if origin_stage not in {"testing", "accepting"}:
            return None
        attribution = self._test_failure_attribution(state)
        if attribution is None or not attribution.is_unrelated_breakage or attribution.primary_failing_test is None:
            return None
        task = get_task_record(root, state.task_id)
        if task is None:
            return None
        created = create_follow_up_tasks(
            root,
            parent_task=task,
            stage=origin_stage,
            follow_ups=[
                build_unrelated_test_follow_up(
                    parent_task_id=task.id,
                    failing_test=attribution.primary_failing_test,
                    changed_files=attribution.changed_files,
                )
            ],
        )
        if not created:
            return None
        follow_up = created[0]
        record_recovery_report(
            root,
            task,
            trigger_event_kind=trigger.trigger_event_kind,
            origin_stage=origin_stage,
            summary=(
                f"Attributed `{attribution.primary_failing_test}` to unrelated breakage, created blocking follow-up "
                f"{follow_up.id}, and flagged the current task."
            ),
            runnable_state="blocked",
            actions=[
                RecoveryAction(
                    action="attribute_test_failure",
                    summary=attribution.reasoning,
                    metadata={
                        "classification": attribution.classification,
                        "failing_tests": list(attribution.failing_tests),
                        "changed_files": list(attribution.changed_files),
                    },
                ),
                RecoveryAction(
                    action="create_follow_up_task",
                    summary=f"Created blocking bugfix task {follow_up.id}: {follow_up.title}",
                    metadata={"follow_up_task_id": follow_up.id},
                ),
                RecoveryAction(
                    action="block_current_task",
                    summary=f"Current task blocked on follow-up {follow_up.id}",
                    metadata={"follow_up_task_id": follow_up.id},
                ),
            ],
            failure_classification=UNRELATED_TEST_BREAKAGE,
            blocker=f"{follow_up.id}: {follow_up.title}",
        )
        return RecoveryFailed(reason=blocked_on_follow_up_reason(follow_up.id))


def _follow_up_stage(origin_stage: str | None) -> str | None:
    if origin_stage in {"before_testing", "testing", "after_testing"}:
        return "testing"
    if origin_stage in {"before_accepting", "accepting", "after_accepting"}:
        return "accepting"
    if origin_stage in {"before_implementing", "implementing", "after_implementing"}:
        return "implementing"
    if origin_stage in {"before_grooming", "grooming", "after_grooming", "recovering"}:
        return "grooming"
    if origin_stage in {"before_commit", "commit", "after_commit", "merge_resolving"}:
        return "commit_to_git"
    return origin_stage
