from pathlib import Path
from typing import Annotated

import typer

from litehive.attention import record_attention
from litehive.cli.common import WorkspaceOption, make_typer, require_subcommand
from litehive.cli.worktree_support import (
    _apply_rescue_candidate,
    _collect_rescue_candidates,
    _require_clean_main_checkout,
    collect_managed_worktrees,
)
from litehive.config.workspace import ensure_workspace
from litehive.domain.task_ops import WorkspaceConflictError
from litehive.git.ops import GitError, remove_worktree
from litehive.state.records import clear_task_worktree_path, get_task, save_task

app = make_typer(invoke_without_command=True)


@app.callback()
def worktree_group(ctx: typer.Context) -> None:
    require_subcommand(ctx)


@app.command("ls", help="List Litehive-managed task worktrees")
def ls(workspace: WorkspaceOption = Path.cwd()) -> int:
    ensure_workspace(workspace)
    worktrees = collect_managed_worktrees(workspace)
    print(f"workspace: {workspace}")
    print(f"worktree_count: {len(worktrees)}")
    if not worktrees:
        print("worktrees: none")
        return 0
    for item in worktrees:
        print()
        print(f"task_id: {item.task_id}")
        print(f"status: {item.status}")
        print(f"change_count: {item.change_count}")
        print(f"worktree_path: {item.worktree_rel}")
        print(f"active: {'yes' if item.active else 'no'}")
    return 0


@app.command("clean", help="Remove Litehive-managed worktrees for closed tasks")
def clean(
    workspace: WorkspaceOption = Path.cwd(),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show planned removals only")] = False,
) -> int:
    ensure_workspace(workspace)
    worktrees = collect_managed_worktrees(workspace)
    candidates = [item for item in worktrees if item.cleanable]
    skipped_active = [item for item in worktrees if item.active]

    print(f"workspace: {workspace}")
    print(f"dry_run: {'yes' if dry_run else 'no'}")
    for item in candidates:
        print(f"would_remove: {item.task_id} {item.status} {item.worktree_rel}")
    for item in skipped_active:
        print(f"skipped_active: {item.task_id} {item.status} {item.worktree_rel}")

    if dry_run:
        print("removed_count: 0")
        print(f"would_remove_count: {len(candidates)}")
        return 0

    failures = []
    removed = []
    deferred = []
    for item in candidates:
        try:
            remove_worktree(workspace, item.worktree_path, force=True)
            task = get_task(workspace, item.task_id)
            if task is not None:
                clear_task_worktree_path(task)
                try:
                    save_task(workspace, task)
                except WorkspaceConflictError:
                    # Defer metadata clearing when workspace is locked by active runner
                    record_attention(
                        workspace,
                        kind="stale_worktree_metadata",
                        title=f"Deferred worktree metadata clearing for {item.task_id}",
                        reason=f"Worktree removed but task metadata clearing deferred due to active runner lock",
                        suggested_action="Wait for runner to finish, then run attention reconciliation",
                        task_id=item.task_id,
                        metadata={"worktree_path": item.worktree_rel, "deferred_operation": "clear_worktree_path"}
                    )
                    deferred.append(item)
                    continue
            removed.append(item)
        except GitError as exc:
            failures.append((item, str(exc)))

    for item in removed:
        print(f"removed: {item.task_id} {item.status} {item.worktree_rel}")
    for item in deferred:
        print(f"deferred_metadata_clear: {item.task_id} {item.status} {item.worktree_rel}")
    for item, message in failures:
        print(f"remove_failed: {item.task_id} {message}")
    print(f"removed_count: {len(removed)}")
    print(f"deferred_count: {len(deferred)}")
    return 1 if failures else 0


@app.command("rescue", help="List or rescue merge-failed worktree commits onto main")
def rescue(
    workspace: WorkspaceOption = Path.cwd(),
    apply: Annotated[bool, typer.Option(help="Cherry-pick eligible commits onto main")] = False,
) -> int:
    ensure_workspace(workspace)
    candidates = _collect_rescue_candidates(workspace)

    print(f"workspace: {workspace}")
    print(f"candidate_count: {len(candidates)}")
    if not candidates:
        print("rescues: none")
        return 0

    if not apply:
        for candidate in candidates:
            print()
            print(f"task_id: {candidate.task_id}")
            print(f"worktree_path: {candidate.worktree_rel}")
            if candidate.commit_shas:
                print(f"commit_count: {len(candidate.commit_shas)}")
                print("commits:")
                for sha in candidate.commit_shas:
                    print(f"  - {sha}")
            else:
                print("commit_count: 0")
                print("commits: (none)")
        return 0

    try:
        _require_clean_main_checkout(workspace)
    except GitError as exc:
        print(f"apply_error: {exc}")
        return 1

    results = [_apply_rescue_candidate(workspace, candidate) for candidate in candidates]
    clean_count = sum(1 for item in results if item.status == "clean")
    already_landed_count = sum(1 for item in results if item.status == "already_landed")
    manual_conflict_count = sum(1 for item in results if item.status == "manual_conflict")
    missing_worktree_count = sum(1 for item in results if item.status == "missing_worktree")
    no_commits_count = sum(1 for item in results if item.status == "no_commits")
    active_task_count = sum(1 for item in results if item.status == "active_task")

    for item in results:
        print()
        print(f"task_id: {item.task_id}")
        print(f"worktree_path: {item.worktree_rel}")
        print(f"status: {item.status}")
        if item.commit_shas:
            print(f"commit_count: {len(item.commit_shas)}")
        if item.head_sha:
            print(f"head_sha: {item.head_sha}")
        if item.message:
            print(f"message: {item.message}")

    print()
    print(f"clean_count: {clean_count}")
    print(f"already_landed_count: {already_landed_count}")
    print(f"manual_conflict_count: {manual_conflict_count}")
    print(f"missing_worktree_count: {missing_worktree_count}")
    print(f"no_commits_count: {no_commits_count}")
    print(f"active_task_count: {active_task_count}")
    return 1 if manual_conflict_count or missing_worktree_count or active_task_count else 0
