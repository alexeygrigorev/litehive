"""Scope analysis for recovery agent: distinguish operator cleanup from SWE scope creep."""

import subprocess
from pathlib import Path
from typing import Any


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
        result = subprocess.run(
            ["git", "diff", "main...HEAD", "--name-status"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise ScopeAnalysisError(f"git diff failed for {workspace_root}: {exc}") from exc

    deleted_files = []
    for line in result.stdout.strip().split("\n"):
        if line.strip() and line.startswith("D\t"):
            deleted_files.append(line[2:])
    return deleted_files


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
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"main:{file_path}"], cwd=workspace_root, capture_output=True, check=False
        )
    except OSError as exc:
        raise ScopeAnalysisError(f"git cat-file failed for {file_path}: {exc}") from exc

    if result.returncode != 0:
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
        subprocess.run(
            ["git", "stash", "push", "-m", "temp-stash-for-scope-analysis"],
            cwd=workspace_root,
            capture_output=True,
            check=False,
        )

        try:
            subprocess.run(["git", "checkout", "main"], cwd=workspace_root, capture_output=True, check=True)

            # Try to run the specific test file
            test_result = subprocess.run(
                ["python", "-m", "pytest", test_file, "-x", "--tb=no", "-q"],
                cwd=workspace_root,
                capture_output=True,
                timeout=30,  # 30 second timeout
                check=False,
            )

            # Test is broken if pytest fails
            is_broken = test_result.returncode != 0

        finally:
            # Return to original branch
            subprocess.run(["git", "checkout", "-"], cwd=workspace_root, capture_output=True, check=False)

            # Restore stashed changes
            subprocess.run(["git", "stash", "pop"], cwd=workspace_root, capture_output=True, check=False)

        return is_broken

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise ScopeAnalysisError(f"could not test {test_file} on main: {exc}") from exc


def _has_syntax_errors_on_main(workspace_root: Path, file_path: str) -> bool:
    """Check if a file has syntax errors on main branch."""
    try:
        result = subprocess.run(
            ["git", "show", f"main:{file_path}"], cwd=workspace_root, capture_output=True, text=True, check=True
        )

        # Try to compile the Python file to check for syntax errors
        if file_path.endswith(".py"):
            try:
                compile(result.stdout, file_path, "exec")
                return False  # No syntax error
            except SyntaxError:
                return True  # Has syntax error

        # For non-Python files, assume they're not broken
        return False

    except (subprocess.CalledProcessError, OSError) as exc:
        raise ScopeAnalysisError(f"could not inspect {file_path} on main: {exc}") from exc


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
