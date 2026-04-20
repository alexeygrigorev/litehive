"""Tests for scope analysis module to distinguish operator cleanup from SWE scope creep."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from litehive.recovery.scope_analysis import (
    ScopeAnalysisResult,
    analyze_scope_changes,
    _get_deleted_files,
    _is_file_broken_on_main,
)


def _setup_git_repo(repo_path: Path) -> None:
    """Set up a git repo with initial commit."""
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, check=True)


def _git_commit(repo_path: Path, message: str) -> None:
    """Add all files and commit."""
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo_path, check=True, capture_output=True)


def test_analyze_scope_changes_no_deletions():
    """Test scope analysis when no files are deleted (normal implementation)."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        _setup_git_repo(repo_path)

        # Create initial files
        (repo_path / "working_code.py").write_text("def hello(): return 'world'")
        _git_commit(repo_path, "initial commit")

        # Add new code without deleting anything
        (repo_path / "new_feature.py").write_text("def new_feature(): return True")
        _git_commit(repo_path, "add new feature")

        result = analyze_scope_changes(repo_path)

        assert result.is_operator_cleanup is True
        assert result.deleted_files == []
        assert result.broken_on_main == []
        assert result.healthy_on_main == []
        assert "No files deleted" in result.reasoning


def test_analyze_scope_changes_operator_cleanup():
    """Test scope analysis for legitimate operator cleanup (deleting broken files)."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        _setup_git_repo(repo_path)

        # Create files on main - one broken, one with syntax errors
        (repo_path / "broken_test.py").write_text("# BROKEN: This test never worked\nimport nonexistent_module\ndef test_broken(): pass")
        (repo_path / "syntax_error.py").write_text("def broken(\n  # missing closing parenthesis")
        (repo_path / "working_code.py").write_text("def hello(): return 'world'")
        _git_commit(repo_path, "initial commit with broken files")

        # Create a branch and delete the broken files (operator cleanup)
        subprocess.run(["git", "checkout", "-b", "cleanup"], cwd=repo_path, check=True, capture_output=True)
        (repo_path / "broken_test.py").unlink()
        (repo_path / "syntax_error.py").unlink()
        _git_commit(repo_path, "remove broken files")

        result = analyze_scope_changes(repo_path)

        assert result.is_operator_cleanup is True
        assert set(result.deleted_files) == {"broken_test.py", "syntax_error.py"}
        assert set(result.broken_on_main) == {"broken_test.py", "syntax_error.py"}
        assert result.healthy_on_main == []
        assert "operator cleanup" in result.reasoning.lower()


def test_analyze_scope_changes_scope_creep():
    """Test scope analysis for SWE scope creep (deleting healthy files)."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        _setup_git_repo(repo_path)

        # Create healthy files on main
        (repo_path / "good_test.py").write_text("def test_feature(): assert True")
        (repo_path / "working_code.py").write_text("def hello(): return 'world'")
        _git_commit(repo_path, "initial commit with healthy files")

        # Create a branch and delete healthy files (scope creep)
        subprocess.run(["git", "checkout", "-b", "bad-swe"], cwd=repo_path, check=True, capture_output=True)
        (repo_path / "good_test.py").unlink()
        _git_commit(repo_path, "delete healthy test to avoid fixing it")

        result = analyze_scope_changes(repo_path)

        assert result.is_operator_cleanup is False
        assert result.deleted_files == ["good_test.py"]
        assert result.broken_on_main == []
        assert result.healthy_on_main == ["good_test.py"]
        assert "scope creep" in result.reasoning.lower()


def test_analyze_scope_changes_mixed():
    """Test scope analysis with both operator cleanup and scope creep."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        _setup_git_repo(repo_path)

        # Create mixed files on main
        (repo_path / "broken_test.py").write_text("# BROKEN: This test is broken\nimport missing_module")
        (repo_path / "good_test.py").write_text("def test_feature(): assert True")
        (repo_path / "working_code.py").write_text("def hello(): return 'world'")
        _git_commit(repo_path, "initial commit with mixed files")

        # Create a branch and delete both broken and healthy files
        subprocess.run(["git", "checkout", "-b", "mixed-changes"], cwd=repo_path, check=True, capture_output=True)
        (repo_path / "broken_test.py").unlink()  # legitimate cleanup
        (repo_path / "good_test.py").unlink()    # scope creep
        _git_commit(repo_path, "remove various files")

        result = analyze_scope_changes(repo_path)

        assert result.is_operator_cleanup is False  # because there are healthy files deleted
        assert set(result.deleted_files) == {"broken_test.py", "good_test.py"}
        assert result.broken_on_main == ["broken_test.py"]
        assert result.healthy_on_main == ["good_test.py"]
        assert "scope creep" in result.reasoning.lower()


def test_get_deleted_files():
    """Test getting list of deleted files."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        _setup_git_repo(repo_path)

        # Create initial files
        (repo_path / "file1.py").write_text("content1")
        (repo_path / "file2.py").write_text("content2")
        (repo_path / "file3.py").write_text("content3")
        _git_commit(repo_path, "initial commit")

        # Create branch and delete some files
        subprocess.run(["git", "checkout", "-b", "deletions"], cwd=repo_path, check=True, capture_output=True)
        (repo_path / "file1.py").unlink()
        (repo_path / "file3.py").unlink()
        _git_commit(repo_path, "delete files")

        deleted_files = _get_deleted_files(repo_path)
        assert set(deleted_files) == {"file1.py", "file3.py"}


def test_is_file_broken_on_main_syntax_error():
    """Test detecting files with syntax errors."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        _setup_git_repo(repo_path)

        # File with syntax error
        (repo_path / "broken.py").write_text("def broken(\n  # missing closing paren")
        _git_commit(repo_path, "add broken file")

        assert _is_file_broken_on_main(repo_path, "broken.py") is True


def test_is_file_broken_on_main_broken_markers():
    """Test detecting files with broken markers."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        _setup_git_repo(repo_path)

        # File with broken markers
        (repo_path / "broken.py").write_text("# BROKEN: This is broken\ndef test(): raise NotImplementedError")
        _git_commit(repo_path, "add broken file")

        assert _is_file_broken_on_main(repo_path, "broken.py") is True


def test_is_file_broken_on_main_empty_file():
    """Test detecting empty or whitespace-only files."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        _setup_git_repo(repo_path)

        # Empty file
        (repo_path / "empty.py").write_text("")
        _git_commit(repo_path, "add empty file")

        assert _is_file_broken_on_main(repo_path, "empty.py") is True


def test_is_file_broken_on_main_healthy_file():
    """Test that healthy files are not considered broken."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        _setup_git_repo(repo_path)

        # Healthy file
        (repo_path / "healthy.py").write_text("def test_feature():\n    assert True\n")
        _git_commit(repo_path, "add healthy file")

        assert _is_file_broken_on_main(repo_path, "healthy.py") is False


def test_is_file_broken_on_main_nonexistent():
    """Test that nonexistent files are considered broken."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        _setup_git_repo(repo_path)

        # Create at least one file for initial commit
        (repo_path / "dummy.txt").write_text("dummy content")
        _git_commit(repo_path, "initial commit")

        assert _is_file_broken_on_main(repo_path, "nonexistent.py") is True


def test_analyze_scope_changes_error_handling():
    """Test that errors are handled gracefully."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir) / "nonexistent"

        result = analyze_scope_changes(repo_path)

        assert result.is_operator_cleanup is False  # conservative default
        assert "Error analyzing scope changes" in result.reasoning


def test_scope_analysis_result_named_tuple():
    """Test the ScopeAnalysisResult named tuple."""
    result = ScopeAnalysisResult(
        is_operator_cleanup=True,
        deleted_files=["file1.py"],
        broken_on_main=["file1.py"],
        healthy_on_main=[],
        reasoning="Test reasoning"
    )

    assert result.is_operator_cleanup is True
    assert result.deleted_files == ["file1.py"]
    assert result.broken_on_main == ["file1.py"]
    assert result.healthy_on_main == []
    assert result.reasoning == "Test reasoning"


@patch('litehive.recovery.scope_analysis._git_command')
def test_analyze_scope_changes_git_error(mock_git_command):
    """Test handling of git command errors."""
    mock_git_command.return_value.returncode = 1
    mock_git_command.return_value.stdout = ""

    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)

        result = analyze_scope_changes(repo_path)

        assert result.is_operator_cleanup is True  # no deletions found
        assert result.deleted_files == []
        assert "No files deleted" in result.reasoning


def test_analyze_scope_changes_uncommitted_deletion_healthy():
    """Test scope analysis for uncommitted deletion of healthy files (scope creep)."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        _setup_git_repo(repo_path)

        # Create healthy file on main
        (repo_path / "good_test.py").write_text("def test_feature(): assert True")
        (repo_path / "working_code.py").write_text("def hello(): return 'world'")
        _git_commit(repo_path, "initial commit with healthy files")

        # Create a branch but don't commit yet
        subprocess.run(["git", "checkout", "-b", "task-branch"], cwd=repo_path, check=True, capture_output=True)

        # Delete healthy file WITHOUT committing (uncommitted deletion)
        (repo_path / "good_test.py").unlink()

        result = analyze_scope_changes(repo_path)

        # Should detect this as scope creep since it's a healthy file deletion
        assert result.is_operator_cleanup is False
        assert result.deleted_files == ["good_test.py"]
        assert result.broken_on_main == []
        assert result.healthy_on_main == ["good_test.py"]
        assert "scope creep" in result.reasoning.lower()


def test_analyze_scope_changes_uncommitted_deletion_broken():
    """Test scope analysis for uncommitted deletion of broken files (operator cleanup)."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        _setup_git_repo(repo_path)

        # Create broken file on main
        (repo_path / "broken_test.py").write_text("# BROKEN: This test never worked\nimport nonexistent_module\ndef test_broken(): pass")
        (repo_path / "working_code.py").write_text("def hello(): return 'world'")
        _git_commit(repo_path, "initial commit with broken files")

        # Create a branch but don't commit yet
        subprocess.run(["git", "checkout", "-b", "cleanup-branch"], cwd=repo_path, check=True, capture_output=True)

        # Delete broken file WITHOUT committing (uncommitted deletion)
        (repo_path / "broken_test.py").unlink()

        result = analyze_scope_changes(repo_path)

        # Should detect this as operator cleanup since it's a broken file
        assert result.is_operator_cleanup is True
        assert result.deleted_files == ["broken_test.py"]
        assert result.broken_on_main == ["broken_test.py"]
        assert result.healthy_on_main == []
        assert "operator cleanup" in result.reasoning.lower()


def test_get_deleted_files_uncommitted():
    """Test getting list of deleted files including uncommitted deletions."""
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        _setup_git_repo(repo_path)

        # Create initial files
        (repo_path / "file1.py").write_text("content1")
        (repo_path / "file2.py").write_text("content2")
        (repo_path / "file3.py").write_text("content3")
        _git_commit(repo_path, "initial commit")

        # Create branch, delete and commit one file
        subprocess.run(["git", "checkout", "-b", "deletions"], cwd=repo_path, check=True, capture_output=True)
        (repo_path / "file1.py").unlink()
        _git_commit(repo_path, "delete file1")

        # Delete another file WITHOUT committing (uncommitted deletion)
        (repo_path / "file2.py").unlink()

        deleted_files = _get_deleted_files(repo_path)
        # Should find both committed and uncommitted deletions
        assert set(deleted_files) == {"file1.py", "file2.py"}