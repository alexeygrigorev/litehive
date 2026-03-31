"""Minimal Textual app for litehive."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Static

from litehive.tasks import list_tasks, load_state


class LitehiveApp(App[None]):
    """Main litehive TUI."""

    TITLE = "litehive"
    SUB_TITLE = "Deterministic Task Workspace"

    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    def __init__(self, workspace: Path, default_mode: str, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.workspace = workspace.resolve()
        self.default_mode = default_mode

    def compose(self) -> ComposeResult:
        state = load_state(self.workspace)
        tasks = list_tasks(self.workspace)
        lines = [
            f"workspace: {self.workspace}",
            f"mode: {self.default_mode}",
            f"active_task_id: {state.active_task_id}",
            "",
            "tasks:",
        ]
        if tasks:
            for task in tasks:
                marker = "*" if task.id == state.active_task_id else "-"
                lines.append(
                    f"{marker} {task.id} [{task.status}/{task.pipeline_status}] {task.title}"
                )
        else:
            lines.append("- no tasks yet")

        yield Header()
        yield Static(
            "\n".join(lines),
            id="main",
        )
        yield Footer()
