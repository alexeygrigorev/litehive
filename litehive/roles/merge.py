from typing import Any

from litehive.domain.common import PipelineState
from litehive.lifecycle.persistence import TaskState
from .base import RoleAgent

ROLE_GUIDANCE = """\
- EXECUTE the merge resolution. Do not just describe it.
- A prior attempt that only printed a plan will fail — the runner verifies `git diff --name-only --diff-filter=U` is empty before accepting your session.
- If the merge has not started yet (no conflict markers, no MERGE_HEAD), the checkout may have dirty local changes that blocked `git merge`. In that case: run `git stash -u`, then `git merge <branch> --no-edit`, then `git stash pop` to restore the local changes. If stash pop causes conflicts, resolve them.
- For each conflicting file, open it and read BOTH `<<<<<<< HEAD` and `=======`/`>>>>>>>` sides.
- Edit the file to combine both sides' intent. Never silently drop either side.
- main has the latest infrastructure state (config, .gitignore, imports) — prefer main there.
- The worktree has the task's feature changes — preserve the feature code.
- For code conflicts (same function modified on both sides), include ALL additions.
- For .gitignore / config conflicts, merge all entries from both sides.
- For lockfiles (`uv.lock`, `package-lock.json`), re-run the tool that generates them (e.g. `uv sync`) rather than hand-merging.
- After editing, run: `git add <every resolved file>`
- Run `git diff --name-only --diff-filter=U`. If any files remain, you are not done — go back to the first step for those files.
- Only when that output is empty, run: `git commit --no-edit`
- Self-check before exiting: run `git diff --name-only --diff-filter=U` one more time. If it prints anything, you have NOT finished — fix it or report failure with a concrete reason.
"""

FRESH_ATTEMPT_GUIDANCE = """\
- Fresh merge-resolution attempt: resolve every listed conflict now and prove the unresolved set is empty before you stop.
"""

RETRY_ATTEMPT_GUIDANCE = """\
- Retry after rejection: read the last rejection carefully before touching the merge state.
- Rerun the cited reproduction or verification commands exactly when they are still applicable, especially conflict checks such as `git diff --name-only --diff-filter=U`.
- Fix every cited unresolved conflict or merge verification failure before you stop.
- Do not escape through `blocked`, stale, or environmental claims without current evidence from this worktree.
"""


class MergeAgent(RoleAgent):
    """Resolves git merge conflicts encountered during the commit stage.

    ``MergeAgent`` is not a pipeline stage on its own — the state machine
    routes merge conflicts into ``recovering``, and the recovery flow (or a
    specialized commit-recovery path) can delegate to an instance of this
    class. It extends ``RoleAgent`` so it shares the same engine/selector/
    session mechanics as every other agent, including the four-layer prompt
    assembly.
    """

    NODE_NAME = PipelineState.MERGE_RESOLVING
    ROLE = "merge-resolver"
    ROLE_INSTRUCTIONS = ROLE_GUIDANCE
    FRESH_ATTEMPT_INSTRUCTIONS = FRESH_ATTEMPT_GUIDANCE
    RETRY_ATTEMPT_INSTRUCTIONS = RETRY_ATTEMPT_GUIDANCE

    def build_prompt(self, state: TaskState) -> dict[str, Any]:
        """Add the conflict file list and merge attempt counter to the base prompt so the resolver agent knows what to fix.

        Called by the lifecycle runner when a commit-stage merge conflict
        routes execution into the merge-resolving node.
        """
        base = super().build_prompt(state)
        if state.merge_context is not None:
            conflict_files = list(state.merge_context.conflict_files)
        else:
            conflict_files = []
        if state.merge_context is not None:
            merge_attempt = state.merge_context.merge_attempt
        else:
            merge_attempt = 1
        base.update(
            {
                "conflict_files": conflict_files,
                "merge_attempt": merge_attempt,
            }
        )
        return base
