"""Detect broken `.venv/bin` entrypoints after `uv cache clean`."""

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import stat
import subprocess
import time

from litehive.config.paths import workspace_path

_PROBE_TIMEOUT_SECONDS = 1.0
_MISSING_TARGET_ERRNOS = {errno.ENOENT, errno.ENOTDIR}


@dataclass(slots=True, frozen=True)
class VenvCheckout:
    checkout_root: Path
    venv_path: Path


@dataclass(slots=True, frozen=True)
class BrokenVenvExecutable:
    checkout: VenvCheckout
    binary_path: Path
    error_detail: str

    @property
    def binary_name(self) -> str:
        return self.binary_path.name


def discover_workspace_venvs(root: Path) -> list[VenvCheckout]:
    """Locate every ``.venv`` the workspace might dispatch into — the main checkout plus every per-task worktree — so the health probe walks all of them in one pass instead of leaving worktree venvs to fail at runtime."""
    root = root.resolve()
    checkouts: dict[Path, VenvCheckout] = {}
    main_venv = root / ".venv"
    if main_venv.is_dir():
        resolved_main = main_venv.resolve()
        checkouts[resolved_main] = VenvCheckout(checkout_root=root, venv_path=resolved_main)

    worktrees_dir = workspace_path(root, "worktrees")
    if worktrees_dir.exists():
        for venv_path in sorted(worktrees_dir.glob("*/.venv")):
            checkout_root = venv_path.parent.resolve()
            if venv_path.is_dir():
                resolved_venv = venv_path.resolve()
                checkouts.setdefault(
                    resolved_venv,
                    VenvCheckout(checkout_root=checkout_root, venv_path=resolved_venv),
                )
    return [checkouts[path] for path in sorted(checkouts)]


def probe_broken_venv_executables(root: Path) -> list[BrokenVenvExecutable]:
    """Run a minimal ``--version`` against every executable in each venv's bin dir to surface the "uv cache clean nuked the symlink target" failure mode before it crashes a subagent launch; daemon startup gates on a clean result."""
    findings: list[BrokenVenvExecutable] = []
    for checkout in discover_workspace_venvs(root):
        bin_dir = _venv_bin_dir(checkout.venv_path)
        if not bin_dir.is_dir():
            continue
        for candidate in _iter_probe_candidates(bin_dir):
            error_detail = _probe_executable(candidate)
            if error_detail is None:
                continue
            findings.append(
                BrokenVenvExecutable(
                    checkout=checkout,
                    binary_path=candidate,
                    error_detail=error_detail,
                )
            )
    return findings


def broken_venv_issue_message(workspace_root: Path, finding: BrokenVenvExecutable) -> str:
    """Format an operator-facing fix recipe for one broken venv entrypoint; embeds the concrete ``uv venv --clear`` command for the affected checkout so the operator can copy-paste rather than reconstruct paths."""
    del workspace_root
    checkout_root = finding.checkout.checkout_root
    venv_path = finding.checkout.venv_path
    local_fix = f"`cd {checkout_root} && uv venv --clear .venv && uv sync --extra dev`"
    return (
        f"BROKEN binary={finding.binary_name} venv={venv_path} checkout={checkout_root}"
        f" — exec failed before the tool could start ({finding.error_detail})."
        f" Recreate the venv with {local_fix}."
    )


def daemon_broken_venv_message(workspace_root: Path, findings: list[BrokenVenvExecutable]) -> str:
    """Aggregate per-finding fix recipes into the single error string the daemon prints when it refuses to start a worker pool over broken venvs."""
    lines = [
        "broken virtualenv entrypoints blocked pool start:",
        *[f"- {broken_venv_issue_message(workspace_root, finding)}" for finding in findings],
    ]
    return "\n".join(lines)


def _venv_bin_dir(venv_path: Path) -> Path:
    if os.name == "nt":
        bin_name = "Scripts"
    else:
        bin_name = "bin"
    return venv_path / bin_name


def _iter_probe_candidates(bin_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    for entry in sorted(bin_dir.iterdir()):
        try:
            mode = entry.lstat().st_mode
        except OSError:
            continue
        if stat.S_ISDIR(mode):
            continue
        if entry.is_symlink() or mode & 0o111:
            candidates.append(entry)
    return candidates


def _probe_executable(binary_path: Path) -> str | None:
    try:
        process = subprocess.Popen(
            [str(binary_path), "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        if exc.errno in _MISSING_TARGET_ERRNOS:
            return exc.strerror or str(exc)
        return None

    deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)

    if process.poll() is None:
        process.kill()
        kill_deadline = time.monotonic() + 1
        while process.poll() is None and time.monotonic() < kill_deadline:
            time.sleep(0.05)
        if process.poll() is None:
            process.terminate()
            term_deadline = time.monotonic() + 1
            while process.poll() is None and time.monotonic() < term_deadline:
                time.sleep(0.05)
        return None
    return None
