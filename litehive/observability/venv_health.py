"""
Detect broken ``.venv/bin`` entrypoints.

``uv cache clean`` removes the cache targets that
``.venv/bin/<tool>`` symlinks point at, leaving the venv with
entrypoints that fail to exec. This module probes each
workspace and per-task-worktree venv before the daemon starts
work so the failure surfaces as a clear diagnostic instead of
crashing a subagent at launch.
"""

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import stat
import subprocess
import time

from litehive.workspace import Workspace

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
        """
        Bare filename of the broken executable.

        Used in the operator-facing diagnostic line; printing
        the full path is noisy when only the binary name
        identifies the problem the operator needs to fix.
        """
        return self.binary_path.name


class WorkspaceVenvHealth:
    """
    Workspace-bound virtualenv health probe.

    Locates each workspace/worktree venv and probes executable
    entrypoints before the daemon starts work.
    """

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def discover_venvs(self) -> list[VenvCheckout]:
        """
        Locate every ``.venv`` the workspace might dispatch into.

        Includes the main checkout's ``.venv`` plus the ``.venv``
        of every per-task worktree.
        """
        checkouts: dict[Path, VenvCheckout] = {}
        main_venv = self.workspace.root / ".venv"
        if main_venv.is_dir():
            resolved_main = main_venv.resolve()
            checkouts[resolved_main] = VenvCheckout(checkout_root=self.workspace.root, venv_path=resolved_main)

        worktrees_dir = self.workspace.runtime_path("worktrees")
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

    def probe_broken_executables(self) -> list[BrokenVenvExecutable]:
        """
        Probe every venv entrypoint with a minimal ``--version`` exec.
        """
        findings: list[BrokenVenvExecutable] = []
        for checkout in self.discover_venvs():
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

    def ensure_ready(self) -> None:
        """
        Refuse daemon startup when a workspace has broken venv entrypoints.
        """
        findings = self.probe_broken_executables()
        if findings:
            raise RuntimeError(daemon_broken_venv_message(findings))


def broken_venv_issue_message(finding: BrokenVenvExecutable) -> str:
    """
    Format an operator-facing fix recipe for one broken venv entrypoint.

    Embeds the concrete ``uv venv --clear`` command for the
    affected checkout so the operator can copy-paste it rather
    than reconstruct paths from a generic instruction.
    """
    checkout_root = finding.checkout.checkout_root
    venv_path = finding.checkout.venv_path
    local_fix = f"`cd {checkout_root} && uv venv --clear .venv && uv sync --extra dev`"
    return (
        f"BROKEN binary={finding.binary_name} venv={venv_path} checkout={checkout_root}"
        f" — exec failed before the tool could start ({finding.error_detail})."
        f" Recreate the venv with {local_fix}."
    )


def daemon_broken_venv_message(findings: list[BrokenVenvExecutable]) -> str:
    """
    Aggregate per-finding fix recipes into one daemon-startup error string.

    The daemon refuses to start a worker pool over broken
    venvs because every subagent launch would crash; this
    helper assembles the message it prints in that case so the
    operator sees every broken venv plus a fix for each in
    one block.
    """
    bullets = _broken_venv_bullets(findings)
    lines = [
        "broken virtualenv entrypoints blocked pool start:",
        *bullets,
    ]
    return "\n".join(lines)


def _broken_venv_bullets(findings: list[BrokenVenvExecutable]) -> list[str]:
    """
    Format each broken-venv finding as a bullet line.

    Caller: :func:`daemon_broken_venv_message`.
    """
    bullets: list[str] = []
    for finding in findings:
        bullets.append(f"- {broken_venv_issue_message(finding)}")
    return bullets


def _venv_bin_dir(venv_path: Path) -> Path:
    """
    Return the platform-specific scripts directory inside a venv.

    ``Scripts`` on Windows, ``bin`` elsewhere. Centralized so
    probe and discovery agree on a single layout assumption;
    inlining the platform check at every call site would make
    a future layout change risky.
    """
    if os.name == "nt":
        bin_name = "Scripts"
    else:
        bin_name = "bin"
    return venv_path / bin_name


def _iter_probe_candidates(bin_dir: Path) -> list[Path]:
    """
    List the entries in a venv bin dir worth probing.

    Yields executable regular files and symlinks (which are
    the ones ``uv cache clean`` typically breaks). Skips
    directories and unstattable entries so a transient
    filesystem error on one entry cannot break the whole
    probe.
    """
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
    """
    Confirm a venv entrypoint can exec by running ``--version``.

    Returns an error detail string on ENOENT/ENOTDIR (the
    specific failure mode that signals a broken symlink
    target after ``uv cache clean``). Returns ``None`` for
    everything else, including timeouts and other runtime
    errors, so we never falsely accuse a working entrypoint of
    being broken.
    """
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
