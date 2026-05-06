"""Merge-resolver subagent invocation.

Lives next to the other agent runners (``litehive.agents``) instead of
inside ``worktree.py`` so the worktree module is about worktrees and
agent-running stays in one place. All git plumbing goes through
``litehive.git.ops``; this module only orchestrates the subagent.
"""

from pathlib import Path

from litehive.config.model import LitehiveConfig
from litehive.container import build_container, build_subagent_manager
from litehive.domain.task import TaskRecord
from litehive.git.ops import GitError, merge_abort, merge_no_edit, unmerged_files
from litehive.tasks.journal import append_journal
from litehive.tasks.recovery_engine import resolve_recovery_engine
from litehive.workspace import Workspace


_MERGE_RESOLVER_ROLE = "merge-resolver"

_MERGE_PROMPT_TEMPLATE = (
    "Git merge conflict while updating task {task_id} worktree to latest main.\n"
    "Conflicting files: {conflicts}\n\n"
    "Resolution rules:\n"
    "- Preserve BOTH sides' intent - combine changes, don't pick one side.\n"
    "- Main branch has latest infrastructure. Worktree has task's feature code.\n"
    "- Never silently drop changes from either side.\n\n"
    "After resolving: git add the files, then git commit --no-edit.\n"
)


def run_worktree_merge_agent(
    workspace: Workspace,
    worktree_path: Path,
    task: TaskRecord,
    main_head: str,
    config: LitehiveConfig | None = None,
) -> None:
    """
    Merge ``main_head`` into ``worktree_path``, running the resolver
    agent on conflicts.

    A clean merge journals success and returns. On a conflict, the
    merge-resolver subagent is launched against the conflicting
    files; if the agent does not clear all conflicts, the merge is
    aborted so the worktree is left in its pre-merge state instead
    of half-resolved.
    """
    root = workspace.root
    merged, message = merge_no_edit(worktree_path, main_head)
    if merged:
        append_journal(workspace, task, "[worktree] Merged main into worktree.")
        return

    conflicts = unmerged_files(worktree_path)
    if not conflicts:
        merge_abort(worktree_path)
        append_journal(
            workspace,
            task,
            f"[worktree] Merge failed (no conflict files detected): {message}",
        )
        return

    append_journal(
        workspace,
        task,
        f"[worktree] Merge conflict on {len(conflicts)} file(s). Launching merge agent.",
    )
    cfg = config or build_container(root).config
    try:
        engine_name, model = resolve_recovery_engine(root, task, cfg)
    except GitError as exc:
        append_journal(workspace, task, f"[worktree] Merge agent unavailable: {exc}")
        return

    subagents = build_subagent_manager(root, execution_root=worktree_path)
    subagents.run(
        task,
        role=_MERGE_RESOLVER_ROLE,
        engine_name=engine_name,
        model=model,
        prompt=_MERGE_PROMPT_TEMPLATE.format(task_id=task.id, conflicts=", ".join(conflicts)),
    )

    if not unmerged_files(worktree_path):
        append_journal(workspace, task, "[worktree] Merge agent resolved conflicts.")
        return
    merge_abort(worktree_path)
    append_journal(workspace, task, "[worktree] Merge agent could not resolve. Worktree kept as-is.")
