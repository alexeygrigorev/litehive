"""CommitToGit stage handler."""

import subprocess
from pathlib import Path

from litehive.git_ops import checkpoint_message, current_head, is_git_repo
from litehive.models import StageReport, TaskRecord
from litehive.subagents import SubagentManager
from litehive.tasks import (
    append_journal,
    get_task_worktree_path,
    save_task,
    set_task_commit_sha,
)

from litehive.config import LitehiveConfig

from ._models import resolve_model


def _commit_to_git_report(
    root: Path,
    execution_root: Path,
    task: TaskRecord,
    *,
    auto_commit_enabled: bool,
    subagents: SubagentManager | None = None,
    config: LitehiveConfig | None = None,
) -> StageReport:
    if not auto_commit_enabled:
        task.status = "done"
        task.pipeline_status = "done"
        save_task(root, task)
        append_journal(root, task, "CommitToGit skipped: auto-commit disabled.")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="pass",
            summary="CommitToGit skipped because auto-commit is disabled",
        )

    if not is_git_repo(root):
        task.status = "done"
        task.pipeline_status = "done"
        save_task(root, task)
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="pass",
            summary="CommitToGit skipped: not a git repository",
        )

    head_before = current_head(root)
    commit_msg = checkpoint_message(task)

    # Step 1: commit everything in the worktree
    if execution_root != root:
        subprocess.run(["git", "add", "-A"], cwd=execution_root, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=execution_root, capture_output=True,
        )

    # Step 2: merge worktree into main
    merge_ok = False
    if execution_root != root:
        wt_head = current_head(execution_root)
        if wt_head:
            # Add and commit any dirty files on main so they don't block merge
            subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
            subprocess.run(["git", "commit", "-m", "chore: sync workspace state"],
                           cwd=root, capture_output=True)
            merge = subprocess.run(
                ["git", "merge", wt_head, "-m", commit_msg, "--no-edit"],
                cwd=root, capture_output=True, text=True,
            )
            if merge.returncode == 0:
                merge_ok = True
            else:
                # Merge failed - try agent resolution (exactly once)
                if subagents is not None:
                    conflict_proc = subprocess.run(
                        ["git", "diff", "--name-only", "--diff-filter=U"],
                        cwd=root, capture_output=True, text=True,
                    )
                    conflicts = [f.strip() for f in conflict_proc.stdout.splitlines() if f.strip()]
                    if conflicts:
                        if task.git.merge_agent_attempts >= 1:
                            append_journal(root, task,
                                f"Merge conflict on {len(conflicts)} file(s). "
                                f"Merge agent already attempted ({task.git.merge_agent_attempts} time(s)) — skipping.")
                        else:
                            task.git.merge_agent_attempts += 1
                            save_task(root, task)
                            append_journal(root, task,
                                f"Merge conflict on {len(conflicts)} file(s). Launching merge agent (attempt {task.git.merge_agent_attempts}).")
                            engine_name = (config.recovery_engine if config and config.recovery_engine
                                           else task.engine or (config.default_engine if config else "codex"))
                            model = resolve_model(task, config, engine_name=engine_name) if config else None
                            subagents.run(
                                task, role="merge-resolver", engine_name=engine_name, model=model,
                                prompt=(
                                    f"Git merge conflict while merging task {task.id} worktree into main.\n"
                                    f"Conflicting files: {', '.join(conflicts)}\n\n"
                                    f"Resolution rules:\n"
                                    f"- You must preserve BOTH sides' intent — combine the changes, don't just pick one side.\n"
                                    f"- The main branch has the latest infrastructure state (config, gitignore, imports). Prefer main for infrastructure files.\n"
                                    f"- The worktree has the task's feature changes. Preserve the feature code.\n"
                                    f"- For code conflicts (e.g. both sides added parameters to the same function), include ALL additions.\n"
                                    f"- For .gitignore or config conflicts, merge all entries from both sides.\n"
                                    f"- Never silently drop changes from either side.\n\n"
                                    f"After resolving, run: git add the resolved files, then git commit --no-edit.\n"
                                ),
                            )
                            # Check if agent resolved it
                            remaining = subprocess.run(
                                ["git", "diff", "--name-only", "--diff-filter=U"],
                                cwd=root, capture_output=True, text=True,
                            )
                            if not remaining.stdout.strip():
                                merge_ok = True
                if not merge_ok:
                    subprocess.run(["git", "merge", "--abort"], cwd=root, capture_output=True)
    else:
        merge_ok = True

    # Step 3: verify new commits landed on main
    head_after = current_head(root)
    if not merge_ok:
        append_journal(root, task, "CommitToGit failed: merge did not produce new commits on main.")
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary="CommitToGit failed: merge did not produce new commits on main",
        )

    # Step 4: delete worktree (merge confirmed)
    if execution_root != root:
        worktree_path = get_task_worktree_path(task)
        if worktree_path:
            wt = (root / worktree_path).resolve()
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                           cwd=root, capture_output=True)
            task.git.worktree_path = None
            task.runtime.git.worktree_path = None

    task.status = "done"
    task.pipeline_status = "done"
    task.git.checkpoint_attempts += 1
    task.git.checkpoint_base_sha = head_before
    set_task_commit_sha(task, head_after)
    save_task(root, task)
    append_journal(root, task, f"CommitToGit complete. Commit: {head_after}")

    # Push to remote
    push = subprocess.run(["git", "push"], cwd=root, capture_output=True, text=True)
    if push.returncode != 0:
        append_journal(root, task, f"Push failed: {push.stderr.strip()}")

    return StageReport(
        task_id=task.id,
        step="commit_to_git",
        verdict="pass",
        summary=f"CommitToGit complete. Commit: {head_after[:8]}",
    )
