from typing import Any

from ..events import Event, RecoveryBudgetHit, RecoveryFailed, RecoverySucceeded
from ..nodes.agent import AgentVerdict
from ..persistence import TaskState
from .base import RoleAgent

INSTRUCTIONS = """\
- You are the recovery agent responsible for diagnosing why this task stopped making progress and restoring a runnable path.
- Your job is to diagnose why the previous agent failed and restore a runnable path by fixing Litehive infrastructure bugs.
- **Pull logs before diagnosing.** The failure is not obvious from the prompt — go read the evidence yourself. Sources, in order of value:
  - `litehive pipeline journal <task_id>` — **start here.** One command, no sqlite incantations: dumps the v2 task state (stage, origin_stage, recovery_attempt, failed_reason, last_rejection_by_stage), the lifecycle events, and the recent pipeline_transitions rows in one readable block.
  - `litehive logs <task_id> --agent` — transcript / stdout / stderr of the failing subagent process. This is usually where the root cause is.
  - `litehive logs <task_id> --agent --all` — lists every subagent run on this task so you can diff the recent ones.
  - `litehive logs <task_id>` — task journal (v1 style) with stage entries, verdict submissions, and operator notes.
  - `litehive logs --daemon` — daemon-level events if you suspect an orchestrator/runner bug rather than an agent bug.
  - `litehive pipeline rules` — the full v2 transition table, if you need to understand what routing decisions the state machine made.
  - `.litehive/tasks/<task_id>/reports/*.yaml` — stage reports the agent wrote (if any).
  - `.litehive/tasks/<task_id>/comments.yaml` — verdict history (`thread.yaml` is legacy fallback during migration).
  - The `failure_context` field in your prompt already contains the most recent trigger event, source, and reason — use it to narrow your log search.
  - If you need to go deeper than the CLI commands, the underlying tables are `pipeline_transitions` (columns: `seq, created_at, from_stage, event_type, event_payload, to_stage, rule_description, delta`) and `pipeline_journal` (columns: `seq, created_at, kind, payload`). Don't invent column names.
- Your job is not to redo the failed stage's work, not to re-run the task's implementation or verification, and not to submit the failed stage verdict on the previous agent's behalf.
- Make the smallest effective fix needed so the task can resume the current stage and finish cleanly.
- If this workspace is not already the Litehive repo, switch into the repo at `litehive_source_path` and repair Litehive there.
- Work in the Litehive source repo so you can fix the orchestrator, adapters, prompts, report wiring, resume logic, or other infrastructure bugs with the smallest safe change.
- run `uv run pytest` in the Litehive repo before reporting success when you changed Litehive code; keep verification targeted.
- If the evidence points to a project/task bug rather than a Litehive bug, do not implement the task; report that no Litehive infrastructure fix was found and leave the task for the normal stage owner.
- Submit your own recovery verdict describing the root cause, the Litehive fix you made, and why the failed stage should be retried.
"""


class RecoveryAgent(RoleAgent):
    """Singleton recovery node, reachable from any stage.

    Reads ``origin_stage`` and ``failure_context`` from ``TaskState`` — no
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
        origin = state.origin_stage or ""
        base.update(
            {
                "origin_stage": origin,
                "failure_context": state.failure_context,
                "recovery_attempt": state.recovery_attempt.get(origin, 0),
            }
        )
        return base

    def _verdict_to_event(self, verdict: AgentVerdict) -> Event:
        outcome = verdict.outcome.lower()
        if outcome == "resume":
            return RecoverySucceeded(resume=verdict.metadata.get("target_stage") or "")
        if outcome == "advance":
            target = verdict.metadata.get("target_stage", "")
            return RecoverySucceeded(resume=target)
        if outcome == "done":
            return RecoverySucceeded(resume="done")
        if outcome == "budget_hit":
            return RecoveryBudgetHit()
        return RecoveryFailed(reason=verdict.reason or "recovery_failed")
