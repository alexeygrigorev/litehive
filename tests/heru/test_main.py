from pathlib import Path

import heru.main as main_module

from heru.base import CLIExecutionResult


class _StubEngine:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        prompt: str,
        cwd: Path,
        *,
        model: str | None = None,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
        emit_unified: bool = True,
    ) -> CLIExecutionResult:
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": cwd,
                "model": model,
                "max_turns": max_turns,
                "resume_session_id": resume_session_id,
                "emit_unified": emit_unified,
            }
        )
        return CLIExecutionResult(
            adapter="codex",
            argv=("codex", "exec"),
            cwd=cwd,
            exit_code=7,
            stdout="stub stdout\n",
            stderr="stub stderr\n",
        )


def test_positional_heru_cli_forwards_prompt_and_options(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    engine = _StubEngine()
    monkeypatch.setattr(main_module, "get_engine", lambda name: engine)

    exit_code = main_module.main(
        [
            "codex",
            "Reply with hi",
            "--cwd",
            str(tmp_path),
            "--model",
            "gpt-5.4",
            "--max-turns",
            "3",
            "--resume-session-id",
            "resume-123",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 7
    assert captured.out == "stub stdout\n"
    assert captured.err == "stub stderr\n"
    assert engine.calls == [
        {
            "prompt": "Reply with hi",
            "cwd": tmp_path.resolve(),
            "model": "gpt-5.4",
            "max_turns": 3,
            "resume_session_id": "resume-123",
            "emit_unified": True,
        }
    ]
