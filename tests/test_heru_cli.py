from pathlib import Path

from heru import ENGINE_CHOICES, get_engine
from heru.base import CLIExecutionResult
from heru.main import main


def test_heru_registry_matches_litehive_surface() -> None:
    from litehive.agents import ENGINE_CHOICES as litehive_engine_choices
    from litehive.agents import get_engine as litehive_get_engine

    assert ENGINE_CHOICES == litehive_engine_choices
    assert type(get_engine("claude")) is type(litehive_get_engine("claude"))


def test_heru_cli_runs_selected_engine(monkeypatch, capsys, tmp_path: Path) -> None:
    engine = get_engine("claude")
    calls: dict[str, object] = {}

    def fake_run(
        prompt: str,
        cwd: Path,
        model: str | None = None,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
    ):
        calls.update(
            prompt=prompt,
            cwd=cwd,
            model=model,
            max_turns=max_turns,
            resume_session_id=resume_session_id,
        )
        return CLIExecutionResult(
            adapter="claude",
            argv=("claude", "-p", prompt),
            cwd=cwd,
            exit_code=0,
            stdout="done\n",
            stderr="",
        )

    monkeypatch.setattr(engine, "run", fake_run)

    exit_code = main(
        [
            "claude",
            "ship it",
            "--cwd",
            str(tmp_path),
            "--model",
            "claude-sonnet-4-20250514",
            "--max-turns",
            "3",
            "--resume",
            "session-123",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "done\n"
    assert captured.err == ""
    assert calls == {
        "prompt": "ship it",
        "cwd": tmp_path.resolve(),
        "model": "claude-sonnet-4-20250514",
        "max_turns": 3,
        "resume_session_id": "session-123",
    }
