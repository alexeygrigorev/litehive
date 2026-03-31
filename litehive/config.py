"""Workspace configuration and bootstrap helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import yaml


@dataclass(slots=True)
class LitehiveConfig:
    default_engine: str = "codex"
    opencode_model: str = "zai-coding-plan/glm-5.1"
    auto_commit: bool = True
    task_mode_name: str = "tasks"
    implementation_mode_name: str = "implementation"


def workspace_dir(root: Path) -> Path:
    return root / ".litehive"


def config_path(root: Path) -> Path:
    return workspace_dir(root) / "config.yaml"


def state_path(root: Path) -> Path:
    return workspace_dir(root) / "state.yaml"


def context_path(root: Path) -> Path:
    return workspace_dir(root) / "context.md"


def ensure_workspace(root: Path, config: LitehiveConfig | None = None) -> Path:
    root = root.resolve()
    base = workspace_dir(root)
    tasks = base / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)

    cfg = config or LitehiveConfig()
    if not config_path(root).exists():
        config_path(root).write_text(yaml.safe_dump(asdict(cfg), sort_keys=False), encoding="utf-8")

    if not state_path(root).exists():
        state_path(root).write_text(
            yaml.safe_dump(
                {
                    "active_task_id": None,
                    "mode": cfg.implementation_mode_name,
                    "queue": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    if not context_path(root).exists():
        context_path(root).write_text(
            "\n".join(
                [
                    "# Litehive Workspace Context",
                    "",
                    "Describe this repository and how subagents should work in it.",
                    "",
                    "## Project",
                    "- Purpose:",
                    "- Main package/module locations:",
                    "- Commands to know:",
                    "",
                    "## Development rules",
                    "- Keep changes scoped to the current task.",
                    "- Prefer targeted tests over broad test suites.",
                    "- Record assumptions clearly in the final report.",
                    "",
                    "## Tool usage",
                    "- Use `uv run pytest -q` for the current smoke test suite.",
                    "- Update litehive task artifacts instead of inventing external state stores.",
                    "- If you add a new command or workflow, document it here for future runs.",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    return base


def load_config(root: Path) -> LitehiveConfig:
    ensure_workspace(root)
    data = yaml.safe_load(config_path(root).read_text(encoding="utf-8")) or {}
    return LitehiveConfig(**data)


def load_context(root: Path) -> str:
    ensure_workspace(root)
    return context_path(root).read_text(encoding="utf-8")
