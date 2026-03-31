"""Minimal Textual app for litehive."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Static

from litehive.observability import render_task_summary
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
        yield Header()
        yield Static("", id="main")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh_view)
        self._refresh_view()

    def _refresh_view(self) -> None:
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
                lines.extend(render_task_summary(task, active=task.id == state.active_task_id))
        else:
            lines.append("- no tasks yet")
        self.query_one("#main", Static).update("\n".join(lines))
