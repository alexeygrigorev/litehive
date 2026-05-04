"""Scope analysis for recovery agent: distinguish operator cleanup from SWE scope creep."""

import subprocess
from pathlib import Path
from typing import Any

from litehive.git.ops import (
    GitError,
    checkout_ref,
    diff_name_status,
    path_exists_in_ref,
    show_at_ref,
    stash_pop,
    stash_push,
)


class ScopeAnalysisError(RuntimeError):
    """Raised when scope analysis cannot inspect the current worktree."""


def analyze_scope_changes(workspace_root: Path, task_id: str) -> dict[str, Any]:
    """Analyze worktree changes to distinguish operator cleanup from SWE scope creep.

    Args:
        workspace_root: Root directory of the workspace
        task_id: ID of the current task

    Returns:
        Dict containing scope analysis with:
        - is_operator_cleanup: bool, True if changes are operator cleanup
        - reasoning: str, explanation of the classification
        - deleted_files: list of deleted file paths
        - broken_on_main: list of files that are broken/failing on main
        - healthy_on_main: list of files that are healthy/passing on main
    """
    try:
        deleted_files = _get_deleted_files(workspace_root)
        if not deleted_files:
            return {
                "is_operator_cleanup": True,
                "reasoning": "No files deleted",
                "deleted_files": [],
                "broken_on_main": [],
                "healthy_on_main": [],
            }

        broken_on_main, healthy_on_main = _classify_deleted_files(workspace_root, deleted_files)
        is_operator_cleanup, reasoning = _classify_changes(deleted_files, broken_on_main, healthy_on_main)
        return {
            "is_operator_cleanup": is_operator_cleanup,
            "reasoning": reasoning,
            "deleted_files": deleted_files,
            "broken_on_main": broken_on_main,
            "healthy_on_main": healthy_on_main,
        }
    except ScopeAnalysisError as exc:
        return {
            "is_operator_cleanup": False,
            "reasoning": f"Scope analysis unavailable: {exc}",
            "deleted_files": [],
            "broken_on_main": [],
            "healthy_on_main": [],
            "diagnostic": {"kind": type(exc).__name__, "message": str(exc)},
        }


def _get_deleted_files(workspace_root: Path) -> list[str]:
    """Get list of files deleted in the worktree compared to main."""
    try:
        entries = diff_name_status(workspace_root, "main...HEAD")
    except GitError as exc:
        raise ScopeAnalysisError(str(exc)) from exc
    return [path for status, path in entries if status == "D"]


def _classify_deleted_files(workspace_root: Path, deleted_files: list[str]) -> tuple[list[str], list[str]]:
    """Classify deleted files as broken vs healthy on main branch.

    Returns:
        Tuple of (broken_on_main, healthy_on_main) file lists
    """
    broken_on_main = []
    healthy_on_main = []

    for file_path in deleted_files:
        if _is_file_broken_on_main(workspace_root, file_path):
            broken_on_main.append(file_path)
        else:
            healthy_on_main.append(file_path)

    return broken_on_main, healthy_on_main


def _is_file_broken_on_main(workspace_root: Path, file_path: str) -> bool:
    """Check if a file is broken/failing on the main branch.

    A file is considered broken if:
    1. It doesn't exist on main
    2. It's a test file that fails when run on main
    3. It has syntax errors or import errors on main
    """
    if not path_exists_in_ref(workspace_root, "main", file_path):
        return True
    if _is_test_file(file_path):
        return _is_test_broken_on_main(workspace_root, file_path)
    return _has_syntax_errors_on_main(workspace_root, file_path)


def _is_test_file(file_path: str) -> bool:
    """Check if a file is a test file based on its path and name."""
    path_parts = file_path.lower().split("/")
    filename = Path(file_path).name.lower()

    return (
        "test" in path_parts
        or "tests" in path_parts
        or filename.startswith("test_")
        or filename.endswith("_test.py")
        or filename.startswith("conftest")
    )


def _is_test_broken_on_main(workspace_root: Path, test_file: str) -> bool:
    """Check if a test file is broken (failing) on main branch."""
    try:
        stash_push(workspace_root, "temp-stash-for-scope-analysis")

        try:
            if not checkout_ref(workspace_root, "main"):
                raise ScopeAnalysisError(f"could not checkout main to test {test_file}")

            test_result = subprocess.run(
                ["python", "-m", "pytest", test_file, "-x", "--tb=no", "-q"],
                cwd=workspace_root,
                capture_output=True,
                timeout=30,
                check=False,
            )

            is_broken = test_result.returncode != 0

        finally:
            checkout_ref(workspace_root, "-")
            stash_pop(workspace_root)

        return is_broken

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise ScopeAnalysisError(f"could not test {test_file} on main: {exc}") from exc


def _has_syntax_errors_on_main(workspace_root: Path, file_path: str) -> bool:
    """Check if a file has syntax errors on main branch."""
    try:
        contents = show_at_ref(workspace_root, "main", file_path)
    except GitError as exc:
        raise ScopeAnalysisError(f"could not inspect {file_path} on main: {exc}") from exc

    if not file_path.endswith(".py"):
        return False
    try:
        compile(contents, file_path, "exec")
        return False
    except SyntaxError:
        return True


def _classify_changes(
    deleted_files: list[str], broken_on_main: list[str], healthy_on_main: list[str]
) -> tuple[bool, str]:
    """Classify the changes as operator cleanup vs SWE scope creep.

    Returns:
        Tuple of (is_operator_cleanup, reasoning)
    """
    total_deleted = len(deleted_files)

    if total_deleted == 0:
        return True, "No files deleted"

    broken_count = len(broken_on_main)
    healthy_count = len(healthy_on_main)

    # If all deleted files were already broken on main, this is operator cleanup
    if healthy_count == 0:
        if broken_count == 1:
            return True, f"Deleted 1 file that was already broken on main: {broken_on_main[0]}"
        else:
            return True, f"Deleted {broken_count} files that were already broken on main"

    # If majority of deleted files were broken, likely operator cleanup
    if broken_count > 0 and broken_count >= healthy_count:
        return True, f"Deleted {broken_count} broken files and {healthy_count} healthy files - majority cleanup"

    # If only healthy files were deleted, this is likely scope creep
    if broken_count == 0:
        if healthy_count == 1:
            return False, f"Deleted 1 healthy file from main: {healthy_on_main[0]} - potential scope creep"
        else:
            return False, f"Deleted {healthy_count} healthy files - potential scope creep"

    # Mixed case - more healthy than broken suggests scope creep
    return False, f"Deleted {healthy_count} healthy files vs {broken_count} broken files - potential scope creep"
