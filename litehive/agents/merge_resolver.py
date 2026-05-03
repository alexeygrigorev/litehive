"""Merge-resolver subagent invocation.

Lives next to the other agent runners (``litehive.agents``) instead of
inside ``worktree.py`` so the worktree module is about worktrees and
agent-running stays in one place. The actual git plumbing — try the
merge, collect conflict files, abort if the agent failed — stays here
because it is specific to this agent's contract.
"""

import subprocess
from pathlib import Path

from litehive.agents.manager import SubagentManager
from litehive.config.loading import load_config
from litehive.config.model import LitehiveConfig
from litehive.domain.task import TaskRecord
from litehive.git.ops import GitError
from litehive.tasks.journal import append_journal
from litehive.tasks.recovery_engine import resolve_recovery_engine


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
    root: Path,
    worktree_path: Path,
    task: TaskRecord,
    main_head: str,
    *,
    config: LitehiveConfig | None = None,
) -> None:
    """Try to merge ``main_head`` into ``worktree_path``; if conflicts, run the resolver agent.

    On a clean merge, journals success and returns. On a conflict, the
    merge-resolver subagent is launched against the conflicting files.
    If the agent does not clear all conflicts, the merge is aborted so
    the worktree is left in its pre-merge state.
    """
    merge = subprocess.run(
        ["git", "merge", main_head, "--no-edit"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if merge.returncode == 0:
        append_journal(root, task, "[worktree] Merged main into worktree.")
        return

    conflicts = _list_unresolved_files(worktree_path)
    if not conflicts:
        subprocess.run(["git", "merge", "--abort"], cwd=worktree_path, capture_output=True)
        append_journal(
            root,
            task,
            f"[worktree] Merge failed (no conflict files detected): {merge.stderr.strip()}",
        )
        return

    append_journal(
        root,
        task,
        f"[worktree] Merge conflict on {len(conflicts)} file(s). Launching merge agent.",
    )
    cfg = config or load_config(root)
    try:
        engine_name, model = resolve_recovery_engine(root, task, cfg)
    except GitError as exc:
        append_journal(root, task, f"[worktree] Merge agent unavailable: {exc}")
        return

    subagents = SubagentManager(root, execution_root=worktree_path)
    subagents.run(
        task,
        role=_MERGE_RESOLVER_ROLE,
        engine_name=engine_name,
        model=model,
        prompt=_MERGE_PROMPT_TEMPLATE.format(task_id=task.id, conflicts=", ".join(conflicts)),
    )

    if not _list_unresolved_files(worktree_path):
        append_journal(root, task, "[worktree] Merge agent resolved conflicts.")
        return
    subprocess.run(["git", "merge", "--abort"], cwd=worktree_path, capture_output=True)
    append_journal(root, task, "[worktree] Merge agent could not resolve. Worktree kept as-is.")


def _list_unresolved_files(worktree_path: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
