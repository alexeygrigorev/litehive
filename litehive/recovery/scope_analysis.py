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
from litehive.workspace import Workspace


class ScopeAnalysisError(RuntimeError):
    """
    Raised when scope analysis cannot inspect the current worktree.

    The recovery agent treats this as a soft failure: scope analysis
    is best-effort context, not a gate, so the agent still proceeds
    but the classification falls back to "scope analysis unavailable"
    rather than letting the inability to classify block recovery.
    """


def analyze_scope_changes(workspace: Workspace) -> dict[str, Any]:
    """
    Classify worktree changes as operator cleanup vs SWE scope creep.

    Used by the recovery agent when triaging deletions: a worktree
    that removed only files already broken on main is operator cleanup,
    one that removed healthy files is scope creep that the agent must
    flag. Returns a dict with the classification, reasoning, and the
    deleted/broken/healthy file lists for the recovery report.
    """
    try:
        try:
            entries = diff_name_status(workspace.root, "main...HEAD")
        except GitError as exc:
            raise ScopeAnalysisError(str(exc)) from exc
        deleted_files = [path for status, path in entries if status == "D"]

        if not deleted_files:
            return {
                "is_operator_cleanup": True,
                "reasoning": "No files deleted",
                "deleted_files": [],
                "broken_on_main": [],
                "healthy_on_main": [],
            }

        broken_on_main: list[str] = []
        healthy_on_main: list[str] = []
        for file_path in deleted_files:
            if _is_file_broken_on_main(workspace, file_path):
                broken_on_main.append(file_path)
            else:
                healthy_on_main.append(file_path)

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


def _is_file_broken_on_main(workspace: Workspace, file_path: str) -> bool:
    """
    Decide whether a file is broken or failing on the main branch.

    A file is considered broken when (a) it doesn't exist on main,
    (b) it's a test file that fails when run on main, or (c) it has
    syntax errors on main. The "already broken" classification is
    what licenses operator cleanup deletions during scope analysis.
    """
    if not path_exists_in_ref(workspace.root, "main", file_path):
        return True
    if _is_test_file(file_path):
        return _is_test_broken_on_main(workspace, file_path)
    return _has_syntax_errors_on_main(workspace, file_path)


def _is_test_file(file_path: str) -> bool:
    """
    Decide whether a path looks like a test file.

    Used to gate the more expensive "run pytest on main" check; the
    pattern set (``test_*.py``, ``*_test.py``, ``conftest.py``,
    paths under ``test``/``tests``) is the convention this codebase
    follows so it's safe to use as the routing predicate.
    """
    path_parts = file_path.lower().split("/")
    filename = Path(file_path).name.lower()

    return (
        "test" in path_parts
        or "tests" in path_parts
        or filename.startswith("test_")
        or filename.endswith("_test.py")
        or filename.startswith("conftest")
    )


def _is_test_broken_on_main(workspace: Workspace, test_file: str) -> bool:
    """
    True when a test file fails when run on main.

    Stash-then-checkout-main is what makes the probe safe against
    uncommitted worktree changes: the worktree's edits are tucked
    away while the test runs, then restored regardless of the test
    outcome so the probe never leaves the worktree dirty.
    """
    try:
        stash_push(workspace.root, "temp-stash-for-scope-analysis")

        try:
            if not checkout_ref(workspace.root, "main"):
                raise ScopeAnalysisError(f"could not checkout main to test {test_file}")

            test_result = subprocess.run(
                ["python", "-m", "pytest", test_file, "-x", "--tb=no", "-q"],
                cwd=workspace.root,
                capture_output=True,
                timeout=30,
                check=False,
            )

            is_broken = test_result.returncode != 0

        finally:
            checkout_ref(workspace.root, "-")
            stash_pop(workspace.root)

        return is_broken

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise ScopeAnalysisError(f"could not test {test_file} on main: {exc}") from exc


def _has_syntax_errors_on_main(workspace: Workspace, file_path: str) -> bool:
    """
    True when the file is Python and parses with a SyntaxError on main.

    Cheaper than the test-runner probe; used as the broken-on-main
    signal for non-test files where actually executing the file is
    not safe. Non-Python files always come back ``False`` because the
    syntax check has nothing to say about them.
    """
    try:
        contents = show_at_ref(workspace.root, "main", file_path)
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
    """
    Classify deletions as operator cleanup or SWE scope creep.

    Returns ``(is_operator_cleanup, reasoning)``: cleanup when every
    deletion was already broken on main (or the broken set dominates),
    scope creep when healthy files were deleted; the reasoning string
    is rendered into the recovery report so the operator can audit.
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
