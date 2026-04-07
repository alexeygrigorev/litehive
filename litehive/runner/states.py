"""Explicit state machine for the task pipeline.

Pipeline stages flow in a fixed order:

    backlog ──► grooming ──► implementing ──► testing ──► accepting ──► commit_to_git ──► done

Rejections and failures from testing and accepting route back to implementing.
Commit failures route to flagged for operator recovery.

The canonical transition table is ``_ROUTES``: ``(stage, verdict) -> next_stage``.
``_STEPS_FROM`` maps a task's ``pipeline_status`` to the stage that should execute next.
"""

from enum import Enum


class PipelineState(str, Enum):
    """All valid pipeline states for a task.

    States fall into three categories:

    * **Runnable stages** – the task is actively progressing through the pipeline.
      These appear as keys in ``_STEPS_FROM`` and generate real stage executions.
    * **Terminal states** – the pipeline has ended.  ``done`` means success;
      ``flagged`` and ``interrupted`` require operator intervention.
    * **Queue states** – transient statuses used by the pool scheduler while a
      task waits for its next turn (``queued``, ``paused``).
    """

    # ── Runnable pipeline stages ─────────────────────────────────────────────
    BACKLOG = "backlog"
    GROOMING = "grooming"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    ACCEPTING = "accepting"
    COMMIT_TO_GIT = "commit_to_git"

    # ── Terminal states ───────────────────────────────────────────────────────
    DONE = "done"
    """Task completed successfully and has been committed to git."""

    FLAGGED = "flagged"
    """Task requires operator attention before it can continue."""

    MERGE_FAILED = "merge_failed"
    """Worktree merge into main failed and could not be resolved."""

    INTERRUPTED = "interrupted"
    """Task was stopped mid-execution; resumable after inspection."""

    RECOVERY_FAILED = "recovery_failed"
    """Recovery agent tried and failed; left for manual resolution."""

    # ── Transient scheduler states ────────────────────────────────────────────
    QUEUED = "queued"
    """Task is waiting in the pool for its next execution slot."""

    PAUSED = "paused"
    """Task is paused at a human checkpoint and will not auto-advance."""


# Maps pipeline_status -> which stage executor to invoke.
# Only runnable stages have an entry; absent means the task is terminal/queued.
_STEPS_FROM: dict[str, str] = {
    "backlog": "grooming",
    "grooming": "grooming",
    "implementing": "implementing",
    "testing": "testing",
    "accepting": "accepting",
    "commit_to_git": "commit_to_git",
}

# Transition table: (current_stage, verdict) -> next_stage
#
# Verdicts:
#   pass / accept  – stage completed successfully → advance to next stage
#   fail / reject  – stage failed or was rejected → route back or to flagged
#   blocked        – stage cannot proceed       → commit_to_git → flagged
#
# Omitted (stage, verdict) pairs are handled by the runner as:
#   - "reject" outside commit_to_git → retry (requeue same stage)
#   - anything else → flagged (unknown transition)
_ROUTES: dict[tuple[str, str], str] = {
    # grooming ──► implementing (on success)
    ("grooming", "pass"): "implementing",
    ("grooming", "accept"): "implementing",
    # implementing ──► testing (on success)
    ("implementing", "pass"): "testing",
    ("implementing", "accept"): "testing",
    # testing ──► accepting (on success) or back to implementing (on failure)
    ("testing", "pass"): "accepting",
    ("testing", "accept"): "accepting",
    ("testing", "fail"): "implementing",
    ("testing", "reject"): "implementing",
    # accepting ──► commit_to_git (on success) or back to implementing (on rejection)
    ("accepting", "pass"): "commit_to_git",
    ("accepting", "accept"): "commit_to_git",
    ("accepting", "fail"): "implementing",
    ("accepting", "reject"): "implementing",
    # commit_to_git ──► done (on success) or merge_failed/flagged (on failure)
    ("commit_to_git", "pass"): "done",
    ("commit_to_git", "accept"): "done",
    ("commit_to_git", "fail"): "merge_failed",
    ("commit_to_git", "reject"): "flagged",
    ("commit_to_git", "blocked"): "flagged",
}

# Single-mode pipeline: backlog ──► implementing ──► commit_to_git ──► done
# The transition from implementing → commit_to_git vs. done is resolved in the runner
# based on whether files_changed is non-empty.
_SINGLE_STEPS_FROM: dict[str, str] = {
    "backlog": "implementing",
    "implementing": "implementing",
    "commit_to_git": "commit_to_git",
}

_SINGLE_ROUTES: dict[tuple[str, str], str] = {
    # implementing ──► commit_to_git (runner may redirect to done when no files changed)
    ("implementing", "pass"): "commit_to_git",
    ("implementing", "accept"): "commit_to_git",
    # commit_to_git ──► done (on success) or merge_failed/flagged (on failure)
    ("commit_to_git", "pass"): "done",
    ("commit_to_git", "accept"): "done",
    ("commit_to_git", "fail"): "merge_failed",
    ("commit_to_git", "reject"): "flagged",
    ("commit_to_git", "blocked"): "flagged",
}
