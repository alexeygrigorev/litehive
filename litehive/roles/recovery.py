from typing import Any
from pathlib import Path

from litehive.lifecycle.events import Event, RecoveryBudgetHit, RecoveryFailed, RecoverySucceeded
from litehive.lifecycle.nodes.agent import AgentVerdict
from litehive.lifecycle.persistence import TaskState
from litehive.recovery.scope_analysis import analyze_scope_changes
from litehive.state.records import get_task_record
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

    def build_prompt(self, state: TaskState) -> dict[str, Any]:
        base = super().build_prompt(state)
        trigger = state.active_recovery_trigger

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
