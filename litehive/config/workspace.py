"""Workspace bootstrap helpers."""

from dataclasses import asdict
from pathlib import Path

import yaml

from litehive.config.model import LitehiveConfig
from litehive.config.paths import (
    config_path,
    context_path,
    state_path,
    workspace_dir,
    workspace_gitignore_path,
)
from litehive.config.profiles import render_context_template


def render_workspace_gitignore() -> str:
    return "\n".join(
        [
            "# Transient litehive runtime state",
            ".lock",
            ".runner.lock",
            "logs/",
            "pool-summary.txt",
            "engine-monitoring.yaml",
            "worktrees/",
            "tasks/*/runtime.yaml",
            "tasks/*/reports/commit_to_git-*.yaml",
            "",
        ]
    )


def ensure_workspace(root: Path, config: LitehiveConfig | None = None) -> Path:
    root = root.resolve()
    base = workspace_dir(root)
    tasks = base / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)

    cfg = config or LitehiveConfig()
    if not config_path(root).exists():
        initial_config = asdict(cfg) if config is not None else {}
        config_path(root).write_text(
            yaml.safe_dump(initial_config, sort_keys=False),
            encoding="utf-8",
        )

    if not state_path(root).exists():
        state_path(root).write_text(
            yaml.safe_dump(
                {
                    "active_task_id": None,
                    "mode": cfg.implementation_mode_name,
                    "queue": [],
                    "pool_stop_reason": None,
                    "next_task_number": 0,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    if not context_path(root).exists():
        context_path(root).write_text(
            render_context_template(cfg.process_profile), encoding="utf-8"
        )

    if not workspace_gitignore_path(root).exists():
        workspace_gitignore_path(root).write_text(
            render_workspace_gitignore(),
            encoding="utf-8",
        )

    return base
