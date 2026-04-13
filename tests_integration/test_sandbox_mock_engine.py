"""Sandbox integration tests using a mock engine binary.

These tests exercise SandboxLauncher through the same adapter path that
real engines (codex, claude) use, but the "engine" is a small bash script
we drop into tmp_path. This makes the tests:

- Deterministic — no dependency on a real LLM CLI being installed.
- Fast — no LLM round-trips.
- Focused — they probe the sandbox boundary, not engine semantics.

Covers two properties the sandbox promises (see T-0286 / T-0303):

1. Outside-workspace filesystem access is blocked. A mock engine running
   under the sandbox must not be able to read the operator's $HOME, other
   users' home directories, or any host path that isn't bind-mounted by
   the bwrap profile.

2. Git access is gated by role. A mock engine in the `swe` role sees no
   `git` binary at any reachable path. The same mock engine in the
   `merge-resolver` role sees a wrapped `git` that allows safe subcommands
   and rejects the destructive denylist.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from litehive.agents.base import CLIInvocation
from litehive.agents.sandbox import SandboxLauncher
from litehive.config import (
    ExternalEngineSandboxConfig,
    ExternalEngineSandboxPolicy,
    LitehiveConfig,
    ensure_workspace,
)

pytestmark = pytest.mark.integration


def _bubblewrap_available() -> str | None:
    runtime = shutil.which("bwrap")
    if runtime is None:
        return None
    probe = subprocess.run(
        [runtime, "--ro-bind", "/", "/", "--", "/bin/true"],
        capture_output=True,
        text=True,
        check=False,
    )
    return runtime if probe.returncode == 0 else None


def _launcher(root: Path) -> SandboxLauncher:
    runtime = _bubblewrap_available()
    if runtime is None:
        pytest.skip("bubblewrap is required for sandbox integration tests")
    config = LitehiveConfig(
        external_engine_sandbox=ExternalEngineSandboxConfig(
            enabled=True,
            backend="bubblewrap",
            runtime_binary=runtime,
            engine_policies={
                "codex": ExternalEngineSandboxPolicy(enabled=True, network_mode="bridge"),
            },
        )
    )
    return SandboxLauncher(root, config)


def _install_mock_engine(bin_dir: Path, script: str) -> Path:
    """Create a small executable that acts as the engine binary.

    The script is plain bash so it runs inside the no-git profile without
    needing python. It exits with the result of running its body.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    mock = bin_dir / "mock-codex"
    mock.write_text(f"#!/bin/bash\nset -u\n{script}\n", encoding="utf-8")
    mock.chmod(mock.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return mock


def _run_mock_engine(
    workspace: Path,
    *,
    role: str,
    script: str,
    monkeypatch: pytest.MonkeyPatch,
) -> subprocess.CompletedProcess[str]:
    bin_dir = workspace / "mock-bin"
    mock = _install_mock_engine(bin_dir, script)
    # Put the mock on the caller's PATH so shutil.which("mock-codex") resolves.
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    launcher = _launcher(workspace)
    env = {
        "HOME": str(workspace),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }
    invocation = CLIInvocation(
        argv=(mock.name,),
        cwd=workspace,
        env=env,
    )
    # engine_name="codex" picks up the sandbox policy; the binary_name is
    # our fake script. This mirrors how the real pipeline would call a
    # future mock-codex engine if we added one to the allowlist.
    wrapped = launcher.wrap_invocation("codex", mock.name, invocation, role=role)
    return subprocess.run(
        wrapped.argv,
        cwd=wrapped.cwd,
        env=wrapped.env,
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "mock@example.com"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Mock"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    (path / "tracked.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, text=True, check=True)


# ── Test 1: outside-workspace isolation ─────────────────────────────────────


def test_mock_engine_cannot_read_operator_home_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sandboxed mock engine must not reach the operator's real home."""
    ensure_workspace(tmp_path)

    real_home = os.path.expanduser("~")
    # Pick a path that genuinely exists in real $HOME so we can distinguish
    # "path blocked by sandbox" from "path does not exist on this host".
    probe_target = None
    for candidate in (".bashrc", ".profile", ".ssh"):
        if (Path(real_home) / candidate).exists():
            probe_target = f"{real_home}/{candidate}"
            break
    if probe_target is None:
        pytest.skip("no probe target exists in real $HOME")

    script = f"""
    set +e
    ls {probe_target} >/tmp/ls.out 2>/tmp/ls.err
    echo LS_RC=$?
    cat {probe_target} >/tmp/cat.out 2>/tmp/cat.err
    echo CAT_RC=$?
    ls {real_home} >/tmp/home.out 2>/tmp/home.err
    echo HOME_RC=$?
    cat /tmp/ls.err /tmp/cat.err /tmp/home.err
    """

    completed = _run_mock_engine(tmp_path, role="swe", script=script, monkeypatch=monkeypatch)

    assert completed.returncode == 0, completed.stderr
    # All three reads must fail because the real host home is not bind-mounted.
    assert "LS_RC=0" not in completed.stdout
    assert "CAT_RC=0" not in completed.stdout
    assert "HOME_RC=0" not in completed.stdout
    # Sanity: an error message about the missing path appears in stderr capture.
    assert "No such file or directory" in completed.stdout or "cannot" in completed.stdout.lower()


def test_mock_engine_cannot_read_arbitrary_host_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts commonly keep secrets under /root, /etc/shadow, etc. A
    sandboxed mock engine must not read any of those. /etc/shadow is root
    owned with mode 640 — the sandbox caller is non-root but the open()
    must still hit EACCES or ENOENT depending on whether /etc is mirrored."""
    ensure_workspace(tmp_path)

    script = """
    set +e
    cat /etc/shadow >/tmp/shadow.out 2>/tmp/shadow.err
    echo SHADOW_RC=$?
    ls /root >/tmp/root.out 2>/tmp/root.err
    echo ROOT_RC=$?
    cat /tmp/shadow.err /tmp/root.err
    """

    completed = _run_mock_engine(tmp_path, role="swe", script=script, monkeypatch=monkeypatch)

    assert completed.returncode == 0, completed.stderr
    assert "SHADOW_RC=0" not in completed.stdout
    assert "ROOT_RC=0" not in completed.stdout


def test_mock_engine_can_read_and_write_its_own_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: the sandbox must allow normal rw access to the
    workspace root so the agent can actually do its job."""
    ensure_workspace(tmp_path)
    (tmp_path / "seed.txt").write_text("seed content\n", encoding="utf-8")

    script = """
    set -e
    cat seed.txt
    echo "written by mock" > written.txt
    cat written.txt
    """

    completed = _run_mock_engine(tmp_path, role="swe", script=script, monkeypatch=monkeypatch)

    assert completed.returncode == 0, completed.stderr
    assert "seed content" in completed.stdout
    assert "written by mock" in completed.stdout
    assert (tmp_path / "written.txt").read_text(encoding="utf-8") == "written by mock\n"


# ── Test 2: git gating by role ──────────────────────────────────────────────


def test_mock_engine_swe_role_has_no_git_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-merge-resolver roles must not see any `git` binary. Not on PATH,
    not via absolute path, not reachable by setting PATH from inside."""
    ensure_workspace(tmp_path)

    script = """
    set +e
    git --version >/tmp/git.out 2>/tmp/git.err
    echo GIT_RC=$?
    /usr/bin/git --version >/tmp/abs.out 2>/tmp/abs.err
    echo ABS_RC=$?
    which git >/tmp/which.out 2>/tmp/which.err
    echo WHICH_RC=$?
    export PATH=/usr/bin:/bin:/usr/local/bin
    git --version >/tmp/env.out 2>/tmp/env.err
    echo ENV_RC=$?
    cat /tmp/git.err /tmp/abs.err /tmp/env.err
    """

    completed = _run_mock_engine(tmp_path, role="swe", script=script, monkeypatch=monkeypatch)

    assert completed.returncode == 0, completed.stderr
    assert "GIT_RC=127" in completed.stdout
    assert "ABS_RC=127" in completed.stdout
    assert "WHICH_RC=1" in completed.stdout
    assert "ENV_RC=127" in completed.stdout


def test_mock_engine_merge_resolver_has_wrapped_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Merge-resolver role sees a git wrapper: safe subcommands work,
    the destructive denylist rejects with a clear message."""
    ensure_workspace(tmp_path)
    _init_repo(tmp_path)

    allowed = _run_mock_engine(
        tmp_path,
        role="merge-resolver",
        script="git --version && git status --short",
        monkeypatch=monkeypatch,
    )
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout.startswith("git version ")

    denied = _run_mock_engine(
        tmp_path,
        role="merge-resolver",
        script="git push --force origin main",
        monkeypatch=monkeypatch,
    )
    assert denied.returncode == 2, denied.stdout
    assert "blocked destructive git command" in denied.stderr

    filter_repo = _run_mock_engine(
        tmp_path,
        role="merge-resolver",
        script="git filter-repo --help",
        monkeypatch=monkeypatch,
    )
    assert filter_repo.returncode == 2
    assert "filter-repo" in filter_repo.stderr
