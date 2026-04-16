from typing import Any

from litehive.lifecycle.persistence import TaskState
from .base import RoleAgent

INSTRUCTIONS = """\
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


class MergeAgent(RoleAgent):
    """Resolves git merge conflicts encountered during the commit stage.

    ``MergeAgent`` is not a pipeline stage on its own — the state machine
    routes merge conflicts into ``recovering``, and the recovery flow (or a
    specialized commit-recovery path) can delegate to an instance of this
    class. It extends ``RoleAgent`` so it shares the same engine/selector/
    session mechanics as every other agent, including the four-layer prompt
    assembly.
    """

    NODE_NAME = "merge_resolving"
    ROLE = "merge-resolver"
    INSTRUCTIONS = INSTRUCTIONS

    def build_prompt(self, state: TaskState) -> dict[str, Any]:
        base = super().build_prompt(state)
        conflict_files = list(state.merge_context.conflict_files) if state.merge_context is not None else []
        merge_attempt = state.merge_context.merge_attempt if state.merge_context is not None else 1
        base.update(
            {
                "conflict_files": conflict_files,
                "merge_attempt": merge_attempt,
            }
        )
        return base
