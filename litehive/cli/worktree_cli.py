from pathlib import Path
from typing import Annotated

import typer

from litehive.cli.common import WorkspaceOption, make_typer, require_subcommand
from litehive.config.workspace import ensure_workspace
from litehive.git.ops import GitError
from litehive.worktree import WorktreeService

app = make_typer(invoke_without_command=True)


@app.callback()
def worktree_group(ctx: typer.Context) -> None:
    """Force the ``worktree`` group to require a subcommand instead of acting bare."""
    require_subcommand(ctx)


@app.command("ls", help="List Litehive-managed task worktrees")
def ls(workspace: WorkspaceOption = Path.cwd()) -> int:
    """
    Operator inventory of the worktrees Litehive currently owns.

    Typical first step when a task seems stuck or a worktree is
    suspected of carrying uncommitted work; the per-row
    ``change_count`` lets the operator spot dirty trees without
    diving into git.
    """
    ensure_workspace(workspace)
    service = WorktreeService(workspace)
    worktrees = service.collect_managed_worktrees()
    print(f"workspace: {workspace}")
    print(f"worktree_count: {len(worktrees)}")
    if not worktrees:
        print("worktrees: none")
        return 0
    for item in worktrees:
        if item.active:
            active_label = "yes"
        else:
            active_label = "no"
        print()
        print(f"task_id: {item.task_id}")
        print(f"status: {item.status}")
        print(f"change_count: {item.change_count}")
        print(f"worktree_path: {item.worktree_rel}")
        print(f"active: {active_label}")
    return 0


@app.command("clean", help="Remove Litehive-managed worktrees for closed tasks")
def clean(
    workspace: WorkspaceOption = Path.cwd(),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show planned removals only")] = False,
) -> int:
    """
    Operator-driven worktree cleanup for closed-task worktrees.

    Removes worktrees whose tasks have already left the active
    set so disk accumulation does not grow unbounded. ``--dry-run``
    previews the planned removals before deleting on disk
    because the operator wants confirmation before destroying
    on-disk evidence.
    """
    ensure_workspace(workspace)
    service = WorktreeService(workspace)
    results = service.remove_cleanable_worktrees(dry_run=dry_run)

    candidates = results["candidates"]
    skipped_active = results["skipped_active"]
    removed = results["removed"]
    deferred = results["deferred"]
    failures = results["failures"]

    if dry_run:
        dry_run_label = "yes"
    else:
        dry_run_label = "no"
    print(f"workspace: {workspace}")
    print(f"dry_run: {dry_run_label}")

    for item in candidates:
        print(f"would_remove: {item.task_id} {item.status} {item.worktree_rel}")
    for item in skipped_active:
        print(f"skipped_active: {item.task_id} {item.status} {item.worktree_rel}")

    if dry_run:
        print("removed_count: 0")
        print(f"would_remove_count: {len(candidates)}")
        return 0

    for item in removed:
        print(f"removed: {item.task_id} {item.status} {item.worktree_rel}")
    for item in deferred:
        print(f"deferred_metadata_clear: {item.task_id} {item.status} {item.worktree_rel}")
    for item, message in failures:
        print(f"remove_failed: {item.task_id} {message}")
    print(f"removed_count: {len(removed)}")
    print(f"deferred_count: {len(deferred)}")
    if failures:
        return 1
    return 0


@app.command("rescue", help="List or rescue flagged merge-recovery worktree commits onto main")
def rescue(
    workspace: WorkspaceOption = Path.cwd(),
    apply: Annotated[bool, typer.Option(help="Cherry-pick eligible commits onto main")] = False,
) -> int:
    """
    Salvage commits stranded in a flagged worktree.

    Cherry-picks eligible commits onto main when the merge-resolver
    could not finish on its own — the recovery tool of last
    resort, since this is the only way work that landed only on a
    task branch makes it back to the operator's main line.
    Without ``--apply``, prints the candidates so the operator can
    audit before mutating main.
    """
    ensure_workspace(workspace)
    service = WorktreeService(workspace)
    candidates = service.collect_rescue_candidates()

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
        service.require_clean_main_checkout()
    except GitError as exc:
        print(f"apply_error: {exc}")
        return 1

    results = [service.apply_rescue_candidate(candidate) for candidate in candidates]
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
    if manual_conflict_count or missing_worktree_count or active_task_count:
        return 1
    return 0
