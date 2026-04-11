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
                # Merge failed - try agent resolution (up to MERGE_AGENT_MAX_ATTEMPTS,
                # swapping engines between attempts so a codex plan-without-execute
                # failure gets a second chance on claude).
                MERGE_AGENT_MAX_ATTEMPTS = 2
                if subagents is not None:
                    conflict_proc = subprocess.run(
                        ["git", "diff", "--name-only", "--diff-filter=U"],
                        cwd=root, capture_output=True, text=True,
                    )
                    conflicts = [f.strip() for f in conflict_proc.stdout.splitlines() if f.strip()]
                    while conflicts and task.git.merge_agent_attempts < MERGE_AGENT_MAX_ATTEMPTS:
                        task.git.merge_agent_attempts += 1
                        save_task(root, task)
                        # Attempt 1: recovery engine (codex). Attempt 2: force claude.
                        from litehive.pipeline.recovery import _resolve_recovery_engine
                        if task.git.merge_agent_attempts == 1:
                            engine_name, model = _resolve_recovery_engine(root, task, config)
                        else:
                            engine_name, model = "claude", None
                        append_journal(root, task,
                            f"Merge conflict on {len(conflicts)} file(s). "
                            f"Launching merge agent attempt {task.git.merge_agent_attempts}/"
                            f"{MERGE_AGENT_MAX_ATTEMPTS} on {engine_name}.")
                        subagents.run(
                            task, role="merge-resolver", engine_name=engine_name, model=model,
                            prompt=(
                                f"EXECUTE the merge resolution. Do not just describe it.\n"
                                f"A prior attempt returned after only printing a plan — the runner "
                                f"will verify `git diff --name-only --diff-filter=U` is empty before "
                                f"accepting your session. If it is not empty, your attempt is a failure "
                                f"regardless of what you said.\n\n"
                                f"Context: merging task {task.id} worktree into main hit a conflict.\n"
                                f"Conflicting files ({len(conflicts)}): {', '.join(conflicts)}\n\n"
                                f"Required steps, in order:\n"
                                f"1. For each conflicting file, open it, read both <<<<<<< HEAD and =======/>>>>>>>  sides.\n"
                                f"2. Edit the file to combine both sides' intent. Never silently drop either side.\n"
                                f"   - main has the latest infrastructure state (config, gitignore, imports) — prefer main there.\n"
                                f"   - The worktree has the task's feature changes — preserve the feature code.\n"
                                f"   - For code conflicts (same function modified on both sides), include ALL additions.\n"
                                f"   - For .gitignore/config conflicts, merge all entries from both sides.\n"
                                f"   - For lockfiles (uv.lock, package-lock.json), re-run the tool that generates them\n"
                                f"     (e.g. `uv sync`) rather than hand-merging.\n"
                                f"3. After editing, run: git add <every resolved file>\n"
                                f"4. Run: git diff --name-only --diff-filter=U\n"
                                f"   - If any files remain in that output, you are not done. Go back to step 1 for those files.\n"
                                f"5. Only when step 4 is empty, run: git commit --no-edit\n"
                                f"6. Verify with: git status (should show 'nothing to commit, working tree clean' or similar)\n\n"
                                f"Self-check before exiting: run `git diff --name-only --diff-filter=U` one more time. "
                                f"If it prints anything, you have NOT finished the task — fix it or report failure with "
                                f"a concrete reason.\n"
                            ),
                        )
                        # Check if agent actually resolved the conflicts on disk.
                        remaining_proc = subprocess.run(
                            ["git", "diff", "--name-only", "--diff-filter=U"],
                            cwd=root, capture_output=True, text=True,
                        )
                        conflicts = [f.strip() for f in remaining_proc.stdout.splitlines() if f.strip()]
                        if not conflicts:
                            merge_ok = True
                            break
                        append_journal(root, task,
                            f"Merge agent attempt {task.git.merge_agent_attempts} "
                            f"returned but {len(conflicts)} file(s) still conflict.")
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
