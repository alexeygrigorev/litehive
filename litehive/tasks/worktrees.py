"""Task worktree helpers, inspection, and execution-root management."""

import shutil
import subprocess
from pathlib import Path, PurePosixPath

import yaml

from litehive.agents.manager import SubagentManager
from litehive.config.loading import load_config
from litehive.config.model import LitehiveConfig
from litehive.config.paths import workspace_path
from litehive.domain.pool import DirtyWorktreeFinding, DirtyWorktreeGateReport
from litehive.domain.task import TaskRecord
from litehive.git.ops import (
    GitError,
    add_worktree,
    current_head,
    is_git_repo,
    rebase_worktree_onto,
    status_porcelain,
)
from litehive.state.records import (
    get_task_worktree_path,
    list_tasks,
    save_task,
    set_task_worktree_path,
)
from litehive.tasks.journal import append_journal


def task_worktree_path(root: Path, task: TaskRecord) -> Path:
    return workspace_path(root, "worktrees") / f"{task.id}-{task.slug}"


def task_worktree_branch(task: TaskRecord) -> str:
    return f"litehive/{task.id}-{task.slug}"


def is_managed_worktree_path(root: Path, worktree_path: str | None) -> bool:
    if not worktree_path:
        return False
    path = Path(worktree_path).expanduser()
    if not path.is_absolute():
        return False
    try:
        return path.resolve().is_relative_to(workspace_path(root, "worktrees").resolve())
    except OSError:
        return False


def resolve_recorded_worktree_path(root: Path, worktree_path: str | None) -> Path | None:
    if not worktree_path:
        return None
    path = Path(worktree_path).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def serialize_worktree_path(path: Path) -> str:
    return str(path.expanduser().resolve())


def ensure_worktree_venv_link(root: Path, worktree_path: Path) -> Path | None:
    main_venv = (root / ".venv").expanduser()
    if not (main_venv.exists() or main_venv.is_symlink()):
        return None

    worktree_venv = worktree_path / ".venv"
    if worktree_venv.is_symlink() and worktree_venv.resolve() == main_venv.resolve():
        return worktree_venv

    if worktree_venv.is_symlink() or worktree_venv.exists():
        if worktree_venv.is_dir() and not worktree_venv.is_symlink():
            shutil.rmtree(worktree_venv)
        else:
            worktree_venv.unlink()

    worktree_venv.symlink_to(main_venv, target_is_directory=main_venv.is_dir())
    return worktree_venv


def git_worktree_blocks_pool(root: Path) -> bool:
    return inspect_dirty_worktree_gate(root).blocks_pool


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
        owners = [task for task in tasks if _task_can_resume_with_owned_dirty_paths(root, task, dirty_entries)]
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
        worktree_path = resolve_recorded_worktree_path(root, get_task_worktree_path(task))
        if worktree_path is None:
            continue
        recorded_path = get_task_worktree_path(task)
        if not worktree_path.exists():
            findings.append(
                DirtyWorktreeFinding(
                    location_kind="task-worktree",
                    ownership="missing-recorded-worktree",
                    task_id=task.id,
                    worktree_path=recorded_path,
                )
            )
            continue
        try:
            worktree_dirty_entries = status_porcelain(worktree_path)
        except GitError:
            findings.append(
                DirtyWorktreeFinding(
                    location_kind="task-worktree",
                    ownership="missing-recorded-worktree",
                    task_id=task.id,
                    worktree_path=recorded_path,
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
                worktree_path=recorded_path,
                dirty_paths=_dirty_entry_paths(worktree_dirty_entries),
            )
        )

    return DirtyWorktreeGateReport(findings=findings)


def _dirty_entry_paths(dirty_entries: list[str]) -> list[str]:
    paths: list[str] = []
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
    placeholders = {"none", "n/a", "-", ""}
    paths: set[PurePosixPath] = set()
    paths.add(PurePosixPath(".litehive") / "tasks" / f"{task.id}-{task.slug}")
    reports_dir = root / ".litehive" / "tasks" / f"{task.id}-{task.slug}" / "reports"
    if reports_dir.exists():
        for report_file in sorted(reports_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(report_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            for changed_file in data.get("files_changed", []) or []:
                stripped = str(changed_file).strip().strip("/")
                if stripped.lower() not in placeholders:
                    paths.add(PurePosixPath(stripped))
    return paths


def _unexpected_dirty_paths(
    dirty_entries: list[str],
    allowed_paths: set[PurePosixPath],
) -> list[str]:
    unexpected = []
    for entry in dirty_entries:
        if len(entry) < 3:
            continue
        raw = entry[3:].strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].replace('\\"', '"')
        if not raw:
            continue
        if "$tmpdir" in raw or raw.startswith("/tmp/"):
            continue
        if raw.startswith(".litehive/"):
            if not any(raw == str(path) or raw.startswith(f"{path}/") for path in allowed_paths):
                continue
        if any(raw == str(path) or raw.startswith(f"{path}/") for path in allowed_paths):
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


def _remove_origin_remote(worktree_path: Path) -> None:
    _ = worktree_path


def _run_worktree_merge_agent(
    root: Path,
    worktree_path: Path,
    task: TaskRecord,
    main_head: str,
    *,
    config: LitehiveConfig | None = None,
) -> None:
    merge = subprocess.run(
        ["git", "merge", main_head, "--no-edit"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if merge.returncode == 0:
        append_journal(root, task, "[worktree] Merged main into worktree.")
        return

    conflict_proc = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    conflicts = [candidate.strip() for candidate in conflict_proc.stdout.splitlines() if candidate.strip()]
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
    from litehive.tasks.recovery_engine import resolve_recovery_engine

    try:
        engine_name, model = resolve_recovery_engine(root, task, cfg)
    except GitError as exc:
        append_journal(root, task, f"[worktree] Merge agent unavailable: {exc}")
        return
    subagents = SubagentManager(root, execution_root=worktree_path)
    subagents.run(
        task,
        role="merge-resolver",
        engine_name=engine_name,
        model=model,
        prompt=(
            f"Git merge conflict while updating task {task.id} worktree to latest main.\n"
            f"Conflicting files: {', '.join(conflicts)}\n\n"
            "Resolution rules:\n"
            "- Preserve BOTH sides' intent - combine changes, don't pick one side.\n"
            "- Main branch has latest infrastructure. Worktree has task's feature code.\n"
            "- Never silently drop changes from either side.\n\n"
            "After resolving: git add the files, then git commit --no-edit.\n"
        ),
    )
    remaining = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if not remaining.stdout.strip():
        append_journal(root, task, "[worktree] Merge agent resolved conflicts.")
        return
    subprocess.run(["git", "merge", "--abort"], cwd=worktree_path, capture_output=True)
    append_journal(root, task, "[worktree] Merge agent could not resolve. Worktree kept as-is.")


def resolve_task_execution_root(
    root: Path,
    task: TaskRecord,
    *,
    config: LitehiveConfig | None = None,
) -> Path:
    if not is_git_repo(root):
        return root

    recorded_path = get_task_worktree_path(task)
    worktree_path = resolve_recorded_worktree_path(root, recorded_path)
    if worktree_path is not None:
        if not worktree_path.exists():
            set_task_worktree_path(task, None)
            save_task(root, task)
        else:
            main_head = current_head(root)
            if main_head:
                rebased = rebase_worktree_onto(worktree_path, main_head)
                if not rebased:
                    append_journal(
                        root,
                        task,
                        f"[worktree] Rebase onto {main_head[:8]} failed. Launching merge agent.",
                    )
                    _run_worktree_merge_agent(root, worktree_path, task, main_head, config=config)
            _remove_origin_remote(worktree_path)
            return worktree_path

    worktree_path = task_worktree_path(root, task)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        shutil.rmtree(worktree_path)
    add_worktree(root, worktree_path, ref=current_head(root) or "HEAD")
    ensure_worktree_venv_link(root, worktree_path)
    _remove_origin_remote(worktree_path)
    set_task_worktree_path(task, serialize_worktree_path(worktree_path))
    save_task(root, task)
    append_journal(root, task, f"Created task worktree at `{get_task_worktree_path(task)}`.")
    return worktree_path
