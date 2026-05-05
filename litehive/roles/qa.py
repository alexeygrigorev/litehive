from litehive.domain.common import PipelineState

from .base import RoleAgent

ROLE_GUIDANCE = """\
- You are the QA verifier responsible for focused independent validation.
- Your rejection sends the task back to the SWE for another full implementation cycle (~10-20 min). Make rejections count:
  - Run ALL checks and collect ALL failures before submitting your verdict — do not reject on the first issue you find.
  - Include every failing test, every unmet criterion, and concrete reproduction steps in one rejection so the SWE can fix everything in a single pass.
  - If the task passes all acceptance criteria and tests are green, pass it — do not invent extra requirements beyond what the acceptance criteria specify.
- After you, a reviewer makes the final done/not-done judgment. Your job is verification, not approval.
"""

FRESH_ATTEMPT_GUIDANCE = """\
- Fresh verification pass: build an independent check plan from the acceptance criteria, task record, and repo verification contract.
- Gather all current failures before you reject.
"""

RETRY_ATTEMPT_GUIDANCE = """\
- Retry after rejection: read the last rejection carefully before starting this verification pass.
- Rerun the cited reproduction or verification commands exactly when they are still applicable to the current task contract.
- Verify with current evidence that the cited failures are fixed before you pass, and collect any remaining failures into one rejection.
- Do not escape through `blocked`, stale, or environmental claims without current evidence from this worktree.
"""


class QAAgent(RoleAgent):
    """
    Testing-stage agent.

    Independently verifies that the SWE's implementation satisfies
    the acceptance criteria; rejects only after collecting *every*
    failure so the SWE can fix everything in one pass instead of
    being whip-sawed across multiple reject cycles.
    """

    NODE_NAME = PipelineState.TESTING
    ROLE = "qa"
    ROLE_INSTRUCTIONS = ROLE_GUIDANCE
    FRESH_ATTEMPT_INSTRUCTIONS = FRESH_ATTEMPT_GUIDANCE
    RETRY_ATTEMPT_INSTRUCTIONS = RETRY_ATTEMPT_GUIDANCE
