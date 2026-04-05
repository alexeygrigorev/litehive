"""CommitToGit stage handler."""

import subprocess
from pathlib import Path

from litehive.git_ops import current_head, is_git_repo
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

    # Step 1: commit everything in the worktree
    if execution_root != root:
        subprocess.run(["git", "add", "-A"], cwd=execution_root, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"litehive: complete {task.id} {task.slug}"],
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
                ["git", "merge", wt_head, "-m", f"litehive: complete {task.id} {task.slug}", "--no-edit"],
                cwd=root, capture_output=True, text=True,
            )
            if merge.returncode == 0:
                merge_ok = True
            else:
                # Merge failed - try agent resolution
                if subagents is not None:
                    conflict_proc = subprocess.run(
                        ["git", "diff", "--name-only", "--diff-filter=U"],
                        cwd=root, capture_output=True, text=True,
                    )
                    conflicts = [f.strip() for f in conflict_proc.stdout.splitlines() if f.strip()]
                    if conflicts:
                        append_journal(root, task,
                            f"Merge conflict on {len(conflicts)} file(s). Launching merge agent.")
                        engine_name = (config.recovery_engine if config and config.recovery_engine
                                       else task.engine or (config.default_engine if config else "codex"))
                        model = resolve_model(task, config, engine_name=engine_name) if config else None
                        subagents.run(
                            task, role="merge-resolver", engine_name=engine_name, model=model,
                            prompt=(
                                f"Git merge conflict. Conflicting files: {', '.join(conflicts)}\n"
                                f"Resolve the conflicts, git add the files, and git commit --no-edit.\n"
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
    if not merge_ok or head_after == head_before:
        # Merge failed or nothing changed - do NOT mark done, do NOT delete worktree
        append_journal(root, task, f"CommitToGit failed: merge did not produce new commits on main.")
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

    task.status = "done"
    task.pipeline_status = "done"
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
