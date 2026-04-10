"""CommitToGit stage handler."""

import subprocess
from pathlib import Path

from litehive.git import checkpoint_message, current_head, is_git_repo
from litehive.models import StageReport, TaskRecord
from litehive.agents import SubagentManager
from litehive.tasks.crud import get_task_worktree_path, save_task, set_task_commit_sha
from litehive.tasks.journal import append_journal

from litehive.config import LitehiveConfig

from ._hooks import _run_runner_hooks_for_stage


def _worktree_patch_already_on_main(root: Path, wt_head: str, main_head: str) -> bool:
    """Return True when every worktree-only commit is already represented on main."""
    try:
        cherry = subprocess.run(
            ["git", "cherry", main_head, wt_head],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if cherry.returncode != 0:
        return False
    lines = [line.strip() for line in cherry.stdout.splitlines() if line.strip()]
    return not lines or all(line.startswith("-") for line in lines)


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
    wt_head: str | None = None

    # Step 1: commit everything in the worktree
    if execution_root != root:
        subprocess.run(["git", "add", "-A"], cwd=execution_root, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=execution_root, capture_output=True,
        )

    # Step 2: merge worktree into main
    merge_ok = False
    main_head_before_merge = head_before
    if execution_root != root:
        wt_head = current_head(execution_root)
        if wt_head:
            # Add and commit any dirty files on main so they don't block merge
            subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
            subprocess.run(["git", "commit", "-m", "chore: sync workspace state"],
                           cwd=root, capture_output=True)
            main_head_before_merge = current_head(root)
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
                            from litehive.pipeline.recovery import _resolve_recovery_engine
                            engine_name, model = _resolve_recovery_engine(task, config)
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
    no_op_reconciled = bool(
        merge_ok
        and execution_root != root
        and wt_head
        and main_head_before_merge
        and head_after == main_head_before_merge
        and wt_head != head_after
        and _worktree_patch_already_on_main(root, wt_head, head_after)
    )
    if not merge_ok:
        append_journal(
            root,
            task,
            "CommitToGit failed: merge conflict prevented integrating task worktree into main.",
        )
        return StageReport(
            task_id=task.id,
            step="commit_to_git",
            verdict="fail",
            summary="CommitToGit failed: merge conflict prevented integrating task worktree into main",
            failure_classification="merge_conflict",
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

    # Populate files_changed from git diff between pre- and post-merge heads.
    files_changed: list[str] = []
    if head_before and head_after and head_before != head_after:
        try:
            diff_result = subprocess.run(
                ["git", "diff", "--name-only", head_before, head_after],
                cwd=root, capture_output=True, text=True, timeout=10,
            )
            if diff_result.returncode == 0:
                files_changed = sorted(
                    f.strip() for f in diff_result.stdout.splitlines()
                    if f.strip() and not f.strip().startswith(".litehive/")
                )
        except (subprocess.TimeoutExpired, OSError):
            pass

    report = StageReport(
        task_id=task.id,
        step="commit_to_git",
        verdict="pass",
        summary=(
            f"CommitToGit reconciled: work already landed on main; no-op merge at {head_after[:8]}"
            if no_op_reconciled
            else f"CommitToGit complete. Commit: {head_after[:8]}"
        ),
        files_changed=files_changed,
    )

    if config is not None:
        post_merge_hook_report = _run_runner_hooks_for_stage(
            root,
            root,
            task,
            step="commit_to_git",
            config=config,
            phase="after_merge",
            report=report,
        )
        if post_merge_hook_report is not None:
            task.status = "queued"
            task.pipeline_status = "implementing"
            task.git.checkpoint_attempts += 1
            task.git.checkpoint_base_sha = head_before
            set_task_commit_sha(task, head_after)
            save_task(root, task)
            append_journal(
                root,
                task,
                "CommitToGit preserved merged main state after failing `after_merge` hook. "
                "Requeued task at `implementing` for follow-up fixes.",
            )
            post_merge_hook_report.verdict = "blocked"
            post_merge_hook_report.retry_decision = "retry"
            post_merge_hook_report.summary = (
                "Post-merge verification failed after merging to main; "
                "task requeued at implementing without reverting the merge"
            )
            return post_merge_hook_report

    task.status = "done"
    task.pipeline_status = "done"
    task.git.checkpoint_attempts += 1
    task.git.checkpoint_base_sha = head_before
    set_task_commit_sha(task, head_after)
    save_task(root, task)
    if no_op_reconciled:
        append_journal(
            root,
            task,
            f"CommitToGit reconciled: work already landed on main; no-op merge at {head_after}.",
        )
    else:
        append_journal(root, task, f"CommitToGit complete. Commit: {head_after}")

    # Push to remote
    push = subprocess.run(["git", "push"], cwd=root, capture_output=True, text=True)
    if push.returncode != 0:
        append_journal(root, task, f"Push failed: {push.stderr.strip()}")

    return report
