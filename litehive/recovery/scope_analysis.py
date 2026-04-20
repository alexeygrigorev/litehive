"""Scope analysis for distinguishing operator cleanup from SWE scope creep."""

import subprocess
from pathlib import Path
from typing import NamedTuple


class ScopeAnalysisResult(NamedTuple):
    """Result of scope creep vs operator cleanup analysis."""

    is_operator_cleanup: bool
    """True if the changes appear to be operator cleanup, False if potential scope creep."""

    deleted_files: list[str]
    """List of files that were deleted relative to main."""

    broken_on_main: list[str]
    """List of deleted files that were already broken/unused on main."""

    healthy_on_main: list[str]
    """List of deleted files that were healthy/functional on main."""

    reasoning: str
    """Human-readable explanation of the classification."""


def _git_command(worktree_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Execute a git command in the worktree and return the result."""
    return subprocess.run(
        ["git", *args],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=False,
    )


def _get_deleted_files(worktree_path: Path) -> list[str]:
    """Get list of files deleted in the worktree compared to main."""
    deleted_files = []

    # Check committed deletions: main...HEAD
    result = _git_command(worktree_path, "diff", "--name-status", "main...HEAD")
    if result.returncode == 0:
        for line in result.stdout.strip().split('\n'):
            if line and line.startswith('D\t'):
                deleted_files.append(line[2:])  # Remove 'D\t' prefix

    # Check uncommitted deletions: working tree vs HEAD
    result = _git_command(worktree_path, "diff", "--name-status", "HEAD")
    if result.returncode == 0:
        for line in result.stdout.strip().split('\n'):
            if line and line.startswith('D\t'):
                filepath = line[2:]  # Remove 'D\t' prefix
                if filepath not in deleted_files:  # Avoid duplicates
                    deleted_files.append(filepath)

    # Also check staged deletions (in case files are staged for deletion)
    result = _git_command(worktree_path, "diff", "--name-status", "--cached", "HEAD")
    if result.returncode == 0:
        for line in result.stdout.strip().split('\n'):
            if line and line.startswith('D\t'):
                filepath = line[2:]  # Remove 'D\t' prefix
                if filepath not in deleted_files:  # Avoid duplicates
                    deleted_files.append(filepath)

    return deleted_files


def _is_file_broken_on_main(worktree_path: Path, filepath: str) -> bool:
    """Check if a file was already broken/unused/dead on main branch."""
    # Check if file exists on main
    result = _git_command(worktree_path, "show", f"main:{filepath}")
    if result.returncode != 0:
        # File doesn't exist on main or can't be accessed - consider it broken
        return True

    file_content = result.stdout

    # Heuristics for detecting broken/dead files:

    # 1. Empty or whitespace-only files
    if not file_content.strip():
        return True

    # 2. Test files that would fail basic syntax checks
    if filepath.endswith(('.py', '.test.py')) and _is_python_file_syntactically_broken(file_content):
        return True

    # 3. Files with obvious TODO/FIXME markers indicating brokenness
    broken_markers = [
        'TODO: fix this broken',
        'FIXME: broken',
        'raise NotImplementedError',
        'assert False, "broken"',
        '# BROKEN:',
        '# TODO: This is broken',
    ]

    content_lower = file_content.lower()
    for marker in broken_markers:
        if marker.lower() in content_lower:
            return True

    # 4. Test files that import non-existent modules (would fail on main)
    if filepath.endswith(('.py', '.test.py')) and _has_broken_imports_on_main(worktree_path, file_content):
        return True

    return False


def _is_python_file_syntactically_broken(content: str) -> bool:
    """Check if Python file has basic syntax errors."""
    try:
        compile(content, '<string>', 'exec')
        return False
    except SyntaxError:
        return True


def _has_broken_imports_on_main(worktree_path: Path, file_content: str) -> bool:
    """Check if file imports modules that don't exist on main."""
    # Simple heuristic: look for import statements of modules that clearly don't exist
    import_lines = [line.strip() for line in file_content.split('\n')
                   if line.strip().startswith(('import ', 'from '))]

    # Look for imports of modules that have patterns suggesting they're broken/test-only
    broken_import_patterns = [
        'import nonexistent',
        'from broken_module',
        'import test_only_module',
        'from missing_package',
    ]

    for import_line in import_lines:
        for pattern in broken_import_patterns:
            if pattern in import_line.lower():
                return True

    return False


def analyze_scope_changes(worktree_path: Path) -> ScopeAnalysisResult:
    """
    Analyze changes in the worktree to distinguish operator cleanup from SWE scope creep.

    Args:
        worktree_path: Path to the task worktree

    Returns:
        ScopeAnalysisResult indicating whether changes appear to be operator cleanup
        (fixing broken/dead code) or potential scope creep (deleting healthy code)
    """
    try:
        worktree_path = Path(worktree_path).resolve()

        deleted_files = _get_deleted_files(worktree_path)

        if not deleted_files:
            # No deletions - not scope creep, likely normal implementation
            return ScopeAnalysisResult(
                is_operator_cleanup=True,
                deleted_files=[],
                broken_on_main=[],
                healthy_on_main=[],
                reasoning="No files deleted - normal implementation changes"
            )

        broken_on_main = []
        healthy_on_main = []

        for filepath in deleted_files:
            if _is_file_broken_on_main(worktree_path, filepath):
                broken_on_main.append(filepath)
            else:
                healthy_on_main.append(filepath)

        # Classification logic:
        # - If ALL deleted files were broken on main -> operator cleanup
        # - If ANY deleted files were healthy on main -> potential scope creep
        is_operator_cleanup = len(healthy_on_main) == 0

        if is_operator_cleanup:
            reasoning = f"All {len(deleted_files)} deleted file(s) were already broken/unused on main - appears to be operator cleanup"
        else:
            reasoning = f"{len(healthy_on_main)} of {len(deleted_files)} deleted files were healthy on main - potential SWE scope creep"

        return ScopeAnalysisResult(
            is_operator_cleanup=is_operator_cleanup,
            deleted_files=deleted_files,
            broken_on_main=broken_on_main,
            healthy_on_main=healthy_on_main,
            reasoning=reasoning
        )

    except Exception as e:
        # On any error, be conservative and assume potential scope creep
        return ScopeAnalysisResult(
            is_operator_cleanup=False,
            deleted_files=[],
            broken_on_main=[],
            healthy_on_main=[],
            reasoning=f"Error analyzing scope changes: {e} - defaulting to scope creep detection"
        )