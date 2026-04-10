import os
import shutil
import subprocess
from pathlib import Path

import pytest

from litehive.agents.base import CLIInvocation
from litehive.agents.sandbox import SandboxLauncher, SandboxProfile, sandbox_profile_for_role
from litehive.config import ExternalEngineSandboxConfig, ExternalEngineSandboxPolicy, LitehiveConfig, ensure_workspace
from litehive.pipeline._worktree import _resolve_task_execution_root
from litehive.sandbox.git_wrapper import _rejection_reason
from litehive.tasks import create_task


def _bubblewrap_launcher(root: Path) -> SandboxLauncher:
    runtime_binary = shutil.which("bwrap")
    if runtime_binary is None:
        pytest.skip("bubblewrap is required for sandbox integration tests")
    probe = subprocess.run(
        [runtime_binary, "--ro-bind", "/", "/", "--", "/bin/true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"bubblewrap is unavailable on this host: {probe.stderr.strip()}")
    config = LitehiveConfig(
        external_engine_sandbox=ExternalEngineSandboxConfig(
            enabled=True,
            backend="bubblewrap",
            runtime_binary=runtime_binary,
            engine_policies={"codex": ExternalEngineSandboxPolicy(enabled=True, network_mode="bridge")},
        )
    )
    return SandboxLauncher(root, config)


def _run_in_sandbox(root: Path, role: str, script: str) -> subprocess.CompletedProcess[str]:
    launcher = _bubblewrap_launcher(root)
    invocation = CLIInvocation(
        argv=("bash", "-lc", script),
        cwd=root,
        env={
            "HOME": str(root),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )
    wrapped = launcher.wrap_invocation("codex", "bash", invocation, role=role)
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
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True, text=True, check=True)
    (path / "tracked.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, text=True, check=True)


def test_sandbox_profile_defaults_closed() -> None:
    assert sandbox_profile_for_role("merge-resolver") is SandboxProfile.MERGE_RESOLVER
    assert sandbox_profile_for_role("swe") is SandboxProfile.NO_GIT
    assert sandbox_profile_for_role("unknown-role") is SandboxProfile.NO_GIT


@pytest.mark.parametrize(
    ("argv", "snippet"),
    [
        (["push", "--force", "origin", "main"], "force or mirror"),
        (["filter-repo", "--help"], "filter-repo"),
        (["reset", "--hard", "origin/main"], "reset --hard"),
        (["remote", "set-url", "origin", "ssh://example/repo"], "set-url origin"),
    ],
)
def test_git_wrapper_rejects_destructive_commands(argv: list[str], snippet: str) -> None:
    reason = _rejection_reason(argv)
    assert reason is not None
    assert snippet in reason


def test_git_wrapper_rejects_cherry_pick_on_protected_checked_out_branch(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    _init_repo(tmp_path)

    reason = _rejection_reason(["cherry-pick", "deadbeef"], cwd=tmp_path)

    assert reason is not None
    assert "protected ref" in reason


def test_no_git_profile_hides_git_from_path_and_absolute_paths(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    completed = _run_in_sandbox(
        tmp_path,
        "swe",
        """
        set +e
        git --version >/tmp/git.out 2>/tmp/git.err
        echo GIT_RC=$?
        which git >/tmp/which.out 2>/tmp/which.err
        echo WHICH_RC=$?
        /usr/bin/git --version >/tmp/abs.out 2>/tmp/abs.err
        echo ABS_RC=$?
        export PATH=/usr/bin:/bin
        export LITEHIVE_FAKE=1
        git --version >/tmp/env.out 2>/tmp/env.err
        echo ENV_RC=$?
        cat /tmp/git.err /tmp/abs.err /tmp/env.err
        """,
    )

    assert completed.returncode == 0
    assert "GIT_RC=127" in completed.stdout
    assert "WHICH_RC=1" in completed.stdout
    assert "ABS_RC=127" in completed.stdout
    assert "ENV_RC=127" in completed.stdout
    assert "not found" in completed.stdout


def test_merge_resolver_profile_allows_safe_git_commands(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    _init_repo(tmp_path)

    completed = _run_in_sandbox(
        tmp_path,
        "merge-resolver",
        "git --version && git status --short",
    )

    assert completed.returncode == 0
    assert completed.stdout.startswith("git version ")


def test_merge_resolver_profile_rejects_force_push_and_logs_attention(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    _init_repo(tmp_path)

    completed = _run_in_sandbox(tmp_path, "merge-resolver", "git push --force origin main")

    assert completed.returncode == 2
    assert "blocked destructive git command" in completed.stderr
    attention_log = tmp_path / ".litehive" / "runtime" / "attention.log"
    assert attention_log.exists()
    assert "push --force origin main" in attention_log.read_text(encoding="utf-8")


def test_merge_resolver_profile_rejects_filter_repo_and_reset_hard_origin(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    _init_repo(tmp_path)

    filter_repo = _run_in_sandbox(tmp_path, "merge-resolver", "git filter-repo --help")
    reset_hard = _run_in_sandbox(tmp_path, "merge-resolver", "git reset --hard origin/main")
    cherry_pick = _run_in_sandbox(tmp_path, "merge-resolver", "git cherry-pick deadbeef")

    assert filter_repo.returncode == 2
    assert "filter-repo" in filter_repo.stderr
    assert reset_hard.returncode == 2
    assert "reset --hard" in reset_hard.stderr
    assert cherry_pick.returncode == 2
    assert "cherry-pick" in cherry_pick.stderr


def test_task_worktree_origin_is_removed_before_handoff(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    _init_repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], cwd=tmp_path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=tmp_path, capture_output=True, text=True, check=True)
    task = create_task(tmp_path, title="Strip origin")

    worktree_root = _resolve_task_execution_root(tmp_path, task)
    remotes = subprocess.run(
        ["git", "remote"],
        cwd=worktree_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert remotes.returncode == 0
    assert "origin" not in remotes.stdout.split()
