"""Git worktree management and dirty-worktree inspection."""

import shutil
import subprocess
from pathlib import Path, PurePosixPath

import yaml

from litehive.config import LitehiveConfig, load_config
from litehive.git import (
    GitError,
    add_worktree,
    current_head,
    has_changes,
    is_git_repo,
    rebase_worktree_onto,
    status_porcelain,
)
from litehive.models import TaskRecord
from litehive.agents import SubagentManager
from litehive.tasks import (
    append_journal,
    get_task,
    get_task_worktree_path,
    list_tasks,
    save_task,
    set_task_worktree_path,
)

from ._models import resolve_model
from ._types import DirtyWorktreeFinding, DirtyWorktreeGateReport


def _git_worktree_is_dirty(root: Path) -> bool:
    return is_git_repo(root) and has_changes(root)


def _git_worktree_blocks_pool(root: Path) -> bool:
    return inspect_dirty_worktree_gate(root).blocks_pool


def _dirty_worktree_owner_task(root: Path) -> TaskRecord | None:
    report = inspect_dirty_worktree_gate(root)
    task_ids = [
        finding.task_id
        for finding in report.findings
        if finding.location_kind == "main-checkout"
        and finding.ownership == "task-owned"
        and finding.task_id
    ]
    if len(task_ids) != 1:
        return None
    return get_task(root, task_ids[0])


def inspect_dirty_worktree_gate(root: Path) -> DirtyWorktreeGateReport:
    if not is_git_repo(root):
        return DirtyWorktreeGateReport()

    findings: list[DirtyWorktreeFinding] = []
    try:
        dirty_entries = status_porcelain(root)
    except GitError:
        return DirtyWorktreeGateReport()

    tasks = list_tasks(root)
    if dirty_entries:
        owners = [
            task
            for task in tasks
            if _task_can_resume_with_owned_dirty_paths(root, task, dirty_entries)
        ]
        finding = DirtyWorktreeFinding(
            location_kind="main-checkout",
            ownership="main-checkout",
            dirty_paths=_dirty_entry_paths(dirty_entries),
        )
        if len(owners) == 1:
            finding.ownership = "task-owned"
            finding.task_id = owners[0].id
            finding.worktree_path = get_task_worktree_path(owners[0])
        elif len(owners) > 1:
            finding.ownership = "ambiguous-ownership"
            finding.task_id = ",".join(task.id for task in owners)
        findings.append(finding)

    for task in tasks:
        worktree_path = get_task_worktree_path(task)
        if not worktree_path:
            continue
        resolved_path = (root / worktree_path).resolve()
        if not resolved_path.exists():
            findings.append(
                DirtyWorktreeFinding(
                    location_kind="task-worktree",
                    ownership="missing-recorded-worktree",
                    task_id=task.id,
                    worktree_path=worktree_path,
                )
            )
            continue
        try:
            worktree_dirty_entries = status_porcelain(resolved_path)
        except GitError:
            findings.append(
                DirtyWorktreeFinding(
                    location_kind="task-worktree",
                    ownership="missing-recorded-worktree",
                    task_id=task.id,
                    worktree_path=worktree_path,
                )
            )
            continue
        if not worktree_dirty_entries:
            continue
        findings.append(
            DirtyWorktreeFinding(
                location_kind="task-worktree",
                ownership="task-owned-worktree",
                task_id=task.id,
                worktree_path=worktree_path,
                dirty_paths=_dirty_entry_paths(worktree_dirty_entries),
            )
        )

    return DirtyWorktreeGateReport(findings=findings)


def _dirty_entry_paths(dirty_entries: list[str]) -> list[str]:
    """Extract bare paths from git status porcelain output lines."""
    paths = []
    for entry in dirty_entries:
        if len(entry) < 3:
            continue
        raw = entry[3:].strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('\\"', '"')
        if raw:
            paths.append(raw)
    return paths


def _allowed_commit_paths(root: Path, task: TaskRecord) -> set[PurePosixPath]:
    """Return paths expected to be dirty at commit time for this task.

    Includes the task's own .litehive directory and any files listed in
    stage reports under files_changed (placeholder entries are filtered out).
    """
    _PLACEHOLDERS = {"none", "n/a", "-", ""}
    paths: set[PurePosixPath] = set()
    paths.add(PurePosixPath(".litehive") / "tasks" / f"{task.id}-{task.slug}")
    reports_dir = root / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    if reports_dir.exists():
        for report_file in sorted(reports_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(report_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                for f in data.get("files_changed", []) or []:
                    stripped = str(f).strip().strip("/")
                    if stripped.lower() not in _PLACEHOLDERS:
                        paths.add(PurePosixPath(stripped))
            except Exception:
                pass
    return paths


def _unexpected_dirty_paths(
    dirty_entries: list[str],
    allowed_paths: set[PurePosixPath],
) -> list[str]:
    """Return dirty paths that are not covered by the allowed set.

    Workspace-internal churn under .litehive/ and stray tmp-path deletions
    are always ignored regardless of the allowed set.
    """
    unexpected = []
    for entry in dirty_entries:
        if len(entry) < 3:
            continue
        raw = entry[3:].strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('\\"', '"')
        if not raw:
            continue
        # Ignore stray tmpdir cleanup entries (deleted temp workspaces)
        if "$tmpdir" in raw or raw.startswith("/tmp/"):
            continue
        # Ignore all .litehive/ workspace-internal churn
        if raw.startswith(".litehive/"):
            if not any(raw == str(p) or raw.startswith(str(p) + "/") for p in allowed_paths):
                continue
        path = PurePosixPath(raw)
        if any(raw == str(p) or raw.startswith(str(p) + "/") for p in allowed_paths):
            continue
        unexpected.append(raw)
    return unexpected


def _task_can_resume_with_owned_dirty_paths(
    root: Path,
    task: TaskRecord,
    dirty_entries: list[str],
) -> bool:
    if task.status != "interrupted":
        return False
    if task.pipeline_status in {"backlog", "done"}:
        return False
    return not _unexpected_dirty_paths(dirty_entries, _allowed_commit_paths(root, task))


def _task_worktree_path(root: Path, task: TaskRecord) -> Path:
    return root / ".litehive" / "worktrees" / f"{task.id}-{task.slug}"


def _run_worktree_merge_agent(
    root: Path, worktree_path: Path, task: TaskRecord, main_head: str,
    *, config: "LitehiveConfig | None" = None,
) -> None:
    """Merge main into worktree, launching a merge agent on conflict."""
    merge = subprocess.run(
        ["git", "merge", main_head, "--no-edit"],
        cwd=worktree_path, capture_output=True, text=True,
    )
    if merge.returncode == 0:
        append_journal(root, task, "[worktree] Merged main into worktree.")
        return

    # Merge conflict — find conflicting files and launch agent
    conflict_proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=worktree_path, capture_output=True, text=True,
    )
    conflicts = [f.strip() for f in conflict_proc.stdout.splitlines() if f.strip()]
    if not conflicts:
        subprocess.run(["git", "merge", "--abort"], cwd=worktree_path, capture_output=True)
        append_journal(root, task, f"[worktree] Merge failed (no conflict files detected): {merge.stderr.strip()}")
        return

    append_journal(root, task, f"[worktree] Merge conflict on {len(conflicts)} file(s). Launching merge agent.")
    cfg = config or load_config(root)
    engine_name = cfg.recovery_engine or task.engine or cfg.default_engine
    model = resolve_model(task, cfg, engine_name=engine_name)
    subagents = SubagentManager(root, execution_root=worktree_path)
    subagents.run(
        task, role="merge-resolver", engine_name=engine_name, model=model,
        prompt=(
            f"Git merge conflict while updating task {task.id} worktree to latest main.\n"
            f"Conflicting files: {', '.join(conflicts)}\n\n"
            f"Resolution rules:\n"
            f"- Preserve BOTH sides' intent — combine changes, don't pick one side.\n"
            f"- Main branch has latest infrastructure. Worktree has task's feature code.\n"
            f"- Never silently drop changes from either side.\n\n"
            f"After resolving: git add the files, then git commit --no-edit.\n"
        ),
    )
    # Check if resolved
    remaining = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=worktree_path, capture_output=True, text=True,
    )
    if not remaining.stdout.strip():
        append_journal(root, task, "[worktree] Merge agent resolved conflicts.")
    else:
        subprocess.run(["git", "merge", "--abort"], cwd=worktree_path, capture_output=True)
        append_journal(root, task, "[worktree] Merge agent could not resolve. Worktree kept as-is.")


def _resolve_task_execution_root(root: Path, task: TaskRecord, *, config: "LitehiveConfig | None" = None) -> Path:
    if not is_git_repo(root):
        return root

    worktree_path_value = get_task_worktree_path(task)
    if worktree_path_value:
        worktree_path = (root / worktree_path_value).resolve()
        if not worktree_path.exists():
            # Worktree was deleted (manual cleanup or prior crash) — clear stale ref and recreate below
            set_task_worktree_path(task, None)
            save_task(root, task)
        else:
            main_head = current_head(root)
            if main_head:
                rebased = rebase_worktree_onto(worktree_path, main_head)
                if not rebased:
                    # Rebase failed — launch merge agent to resolve
                    append_journal(root, task, f"[worktree] Rebase onto {main_head[:8]} failed. Launching merge agent.")
                    _run_worktree_merge_agent(root, worktree_path, task, main_head, config=config)
            return worktree_path

    worktree_path = _task_worktree_path(root, task)  # noqa: E305
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        shutil.rmtree(worktree_path)
    add_worktree(root, worktree_path, ref=current_head(root) or "HEAD")
    set_task_worktree_path(task, str(worktree_path.relative_to(root)))
    save_task(root, task)
    append_journal(root, task, f"Created task worktree at `{get_task_worktree_path(task)}`.")
    return worktree_path
