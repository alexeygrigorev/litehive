"""Integration tests for sandbox functionality."""

from pathlib import Path
from unittest.mock import patch
import tempfile

import pytest

from heru.base import CLIInvocation
from litehive.agents.sandbox import SandboxLauncher, SandboxProfile, sandbox_profile_for_role
from litehive.config.model import LitehiveConfig, ExternalEngineSandboxConfig, ExternalEngineSandboxPolicy


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace for testing."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield Path(tmp_dir)


@pytest.fixture
def docker_sandbox_config():
    """Create a sandbox config with Docker backend."""
    return LitehiveConfig(
        external_engine_sandbox=ExternalEngineSandboxConfig(
            enabled=True,
            backend="docker",
            runtime_binary="docker",
            image="litehive-external-engine:latest",
            engine_policies={
                "codex": ExternalEngineSandboxPolicy(
                    enabled=True,
                    network_mode="none",
                    workspace_mode="rw",
                    environment=["CODEX_API_KEY"],
                )
            },
        )
    )


def test_sandbox_profile_for_role():
    """Test that roles map to correct sandbox profiles."""
    assert sandbox_profile_for_role("merge-resolver") == SandboxProfile.MERGE_RESOLVER
    assert sandbox_profile_for_role("swe") == SandboxProfile.NO_GIT
    assert sandbox_profile_for_role("qa") == SandboxProfile.NO_GIT
    assert sandbox_profile_for_role("") == SandboxProfile.NO_GIT


def test_sandbox_launcher_policy_summary_disabled(temp_workspace):
    """Test policy summary when sandbox is disabled."""
    config = LitehiveConfig()  # Sandbox disabled by default
    launcher = SandboxLauncher(temp_workspace, config)

    summary = launcher.policy_summary("codex")
    assert not summary.enabled
    assert summary.summary == "host"


def test_sandbox_launcher_policy_summary_enabled(temp_workspace, docker_sandbox_config):
    """Test policy summary when sandbox is enabled."""
    launcher = SandboxLauncher(temp_workspace, docker_sandbox_config)

    summary = launcher.policy_summary("codex")
    assert summary.enabled
    assert summary.backend == "docker"
    assert summary.runtime == "docker"
    assert summary.image == "litehive-external-engine:latest"
    assert summary.network_mode == "none"
    assert summary.workspace_mode == "rw"


@patch('shutil.which')
def test_docker_sandbox_wraps_invocation(mock_which, temp_workspace, docker_sandbox_config):
    """Test that Docker sandbox properly wraps invocations."""
    # Mock docker and codex binaries as available
    mock_which.side_effect = lambda name: {
        "docker": "/usr/bin/docker",
        "codex": "/usr/local/bin/codex",
        "git": "/usr/bin/git",
    }.get(name)

    launcher = SandboxLauncher(temp_workspace, docker_sandbox_config)

    original_invocation = CLIInvocation(
        argv=("/usr/local/bin/codex", "chat", "hello"),
        cwd=temp_workspace,
        env={"CODEX_API_KEY": "test-key"},
    )

    wrapped = launcher.wrap_invocation("codex", "codex", original_invocation)

    # Should return a Docker invocation
    assert wrapped.argv[0] == "docker"
    assert "run" in wrapped.argv
    assert "--rm" in wrapped.argv
    assert "--init" in wrapped.argv
    assert "litehive-external-engine:latest" in wrapped.argv


@patch('shutil.which')
def test_docker_sandbox_git_wrapper_no_git_role(mock_which, temp_workspace, docker_sandbox_config):
    """Test that non-merge-resolver roles get no-git wrapper."""
    mock_which.side_effect = lambda name: {
        "docker": "/usr/bin/docker",
        "codex": "/usr/local/bin/codex",
        "git": "/usr/bin/git",
    }.get(name)

    launcher = SandboxLauncher(temp_workspace, docker_sandbox_config)

    original_invocation = CLIInvocation(
        argv=("/usr/local/bin/codex", "chat"),
        cwd=temp_workspace,
        env={"CODEX_API_KEY": "test-key"},
    )

    wrapped = launcher.wrap_invocation("codex", "codex", original_invocation, role="swe")

    # Should mount the no-git wrapper
    mount_specs = [arg for i, arg in enumerate(wrapped.argv) if wrapped.argv[i-1] == "--mount"]
    git_mounts = [spec for spec in mount_specs if "/usr/local/bin/git" in spec]
    assert len(git_mounts) == 1
    assert "no-git" in git_mounts[0]


@patch('shutil.which')
def test_docker_sandbox_git_wrapper_merge_resolver_role(mock_which, temp_workspace, docker_sandbox_config):
    """Test that merge-resolver role gets merge-git wrapper."""
    mock_which.side_effect = lambda name: {
        "docker": "/usr/bin/docker",
        "codex": "/usr/local/bin/codex",
        "git": "/usr/bin/git",
    }.get(name)

    launcher = SandboxLauncher(temp_workspace, docker_sandbox_config)

    original_invocation = CLIInvocation(
        argv=("/usr/local/bin/codex", "chat"),
        cwd=temp_workspace,
        env={"CODEX_API_KEY": "test-key"},
    )

    wrapped = launcher.wrap_invocation("codex", "codex", original_invocation, role="merge-resolver")

    # Should mount the merge-git wrapper and related dependencies
    mount_specs = [arg for i, arg in enumerate(wrapped.argv) if wrapped.argv[i-1] == "--mount"]
    git_mounts = [spec for spec in mount_specs if "/usr/local/bin/git" in spec]
    assert len(git_mounts) == 1
    assert "merge-git" in git_mounts[0]

    # Should have environment variables for git wrapper
    env_specs = [arg for i, arg in enumerate(wrapped.argv) if wrapped.argv[i-1] == "--env"]
    env_dict = {spec.split("=", 1)[0]: spec.split("=", 1)[1] for spec in env_specs if "=" in spec}
    assert "LITEHIVE_REAL_GIT_PATH" in env_dict
    assert "LITEHIVE_WORKSPACE_ROOT" in env_dict
    assert "LITEHIVE_SOURCE_ROOT" in env_dict


def test_docker_sandbox_unsupported_backend_raises_error():
    """Test that unsupported backends raise an error at config validation."""
    with pytest.raises(ValueError) as exc_info:
        LitehiveConfig(
            external_engine_sandbox=ExternalEngineSandboxConfig(
                enabled=True,
                backend="unsupported",
            )
        )

    assert "external_engine_sandbox.backend must be one of: docker" in str(exc_info.value)


@patch('shutil.which')
def test_docker_sandbox_creates_git_wrappers(mock_which, temp_workspace, docker_sandbox_config):
    """Test that git wrapper scripts are created."""
    mock_which.side_effect = lambda name: {
        "docker": "/usr/bin/docker",
        "codex": "/usr/local/bin/codex",
        "git": "/usr/bin/git",
    }.get(name)

    launcher = SandboxLauncher(temp_workspace, docker_sandbox_config)

    # Trigger wrapper creation
    wrappers = launcher._ensure_docker_git_wrappers()

    assert "merge_git" in wrappers
    assert "no_git" in wrappers

    # Check that files were created and are executable
    merge_git = wrappers["merge_git"]
    no_git = wrappers["no_git"]

    assert merge_git.exists()
    assert no_git.exists()
    assert oct(merge_git.stat().st_mode)[-3:] == "755"
    assert oct(no_git.stat().st_mode)[-3:] == "755"

    # Check content
    merge_content = merge_git.read_text()
    no_git_content = no_git.read_text()

    assert "litehive.sandbox.git_wrapper" in merge_content
    assert "git: not found" in no_git_content