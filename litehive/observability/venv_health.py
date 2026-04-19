"""Detect broken `.venv/bin` entrypoints after `uv cache clean`."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import stat
import subprocess

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
    root = root.resolve()
    checkouts: dict[Path, VenvCheckout] = {}
    main_venv = root / ".venv"
    if main_venv.is_dir():
        checkouts[root] = VenvCheckout(checkout_root=root, venv_path=main_venv)

    worktrees_dir = workspace_path(root, "worktrees")
    if worktrees_dir.exists():
        for venv_path in sorted(worktrees_dir.glob("*/.venv")):
            checkout_root = venv_path.parent.resolve()
            if venv_path.is_dir():
                checkouts[checkout_root] = VenvCheckout(checkout_root=checkout_root, venv_path=venv_path.resolve())
    return [checkouts[path] for path in sorted(checkouts)]


def probe_broken_venv_executables(root: Path) -> list[BrokenVenvExecutable]:
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
    lines = [
        "broken virtualenv entrypoints blocked pool start:",
        *[f"- {broken_venv_issue_message(workspace_root, finding)}" for finding in findings],
    ]
    return "\n".join(lines)


def _venv_bin_dir(venv_path: Path) -> Path:
    return venv_path / ("Scripts" if os.name == "nt" else "bin")


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
        subprocess.run(
            [str(binary_path), "--version"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError as exc:
        if exc.errno in _MISSING_TARGET_ERRNOS:
            return exc.strerror or str(exc)
        return None
    return None
