"""Public CLI entrypoint for heru.

This module owns the stable ``heru <engine> <prompt>`` command shape and
the legacy ``--engine`` compatibility path documented in the README API
Contract section.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import click
import typer

from heru import ENGINE_CHOICES, get_engine
from heru.base import LATEST_CONTINUATION_SENTINEL
from heru import quota

app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
)

USAGE_PROVIDER_CHOICES = ("codex", "claude", "copilot", "zai", "gemini")
SUPPORTED_USAGE_PROVIDERS = ("codex", "claude", "copilot", "zai")


def _usage_provider_error(name: str) -> click.ClickException:
    valid_names = ", ".join(USAGE_PROVIDER_CHOICES)
    return click.ClickException(
        f"Unknown provider '{name}'. Valid provider names: {valid_names}."
    )


def _usage_window_record(
    *,
    provider: str,
    status: str,
    used: float | int | None,
    limit: float | int | None,
    remaining: float | int | None,
    unit: str | None,
    reset_window: str | None,
    reset_at: str | None,
    block_reason: str | None,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "status": status,
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "unit": unit,
        "reset_window": reset_window,
        "reset_at": reset_at,
        "block_reason": block_reason,
        "error": error,
        "details": details or {},
    }


def _pick_named_window(windows: dict[str, Any]) -> tuple[str, Any]:
    return max(windows.items(), key=lambda item: item[1].used_percent)


def _normalize_usage_provider(provider: str) -> dict[str, Any]:
    if provider == "gemini":
        return _usage_window_record(
            provider="gemini",
            status="unsupported",
            used=None,
            limit=None,
            remaining=None,
            unit=None,
            reset_window=None,
            reset_at=None,
            block_reason=None,
            error="unsupported",
        )

    if provider == "codex":
        status_obj = quota.check_codex_quota()
        block_reason = quota.codex_quota_block_reason()
        windows = {
            "primary_window": status_obj.primary_window,
            "secondary_window": status_obj.secondary_window,
        }
        selected_name, selected_window = _pick_named_window(windows)
        status = "error" if status_obj.error else "blocked" if block_reason else "ok"
        used = round(selected_window.used_percent, 2)
        limit = 100.0
        remaining = round(max(0.0, limit - used), 2)
        return _usage_window_record(
            provider=provider,
            status=status,
            used=None if status_obj.error else used,
            limit=None if status_obj.error else limit,
            remaining=None if status_obj.error else remaining,
            unit="percent",
            reset_window=selected_name,
            reset_at=selected_window.reset_at or status_obj.earliest_reset_at,
            block_reason=block_reason,
            error=status_obj.error,
            details={
                "limit_reached": status_obj.limit_reached,
                "windows": {
                    "primary_window": asdict(status_obj.primary_window),
                    "secondary_window": asdict(status_obj.secondary_window),
                },
            },
        )

    if provider == "claude":
        status_obj = quota.check_claude_quota()
        block_reason = quota.claude_quota_block_reason()
        windows = {
            "five_hour": status_obj.five_hour,
            "seven_day": status_obj.seven_day,
        }
        selected_name, selected_window = _pick_named_window(windows)
        status = "error" if status_obj.error else "blocked" if block_reason else "ok"
        used = round(selected_window.used_percent, 2)
        limit = 100.0
        remaining = round(max(0.0, limit - used), 2)
        return _usage_window_record(
            provider=provider,
            status=status,
            used=None if status_obj.error else used,
            limit=None if status_obj.error else limit,
            remaining=None if status_obj.error else remaining,
            unit="percent",
            reset_window=selected_name,
            reset_at=selected_window.reset_at,
            block_reason=block_reason,
            error=status_obj.error,
            details={
                "limit_reached": status_obj.limit_reached,
                "subscription": status_obj.subscription,
                "windows": {
                    "five_hour": asdict(status_obj.five_hour),
                    "seven_day": asdict(status_obj.seven_day),
                },
            },
        )

    if provider == "copilot":
        status_obj = quota.check_copilot_quota()
        block_reason = quota.copilot_quota_block_reason()
        status = "error" if status_obj.error else "blocked" if block_reason else "ok"
        used = round(status_obj.used_percent, 2)
        limit = status_obj.premium_entitlement
        remaining = status_obj.premium_remaining
        return _usage_window_record(
            provider=provider,
            status=status,
            used=None if status_obj.error else used,
            limit=None if status_obj.error else limit,
            remaining=None if status_obj.error else remaining,
            unit="premium_interactions",
            reset_window="monthly",
            reset_at=status_obj.quota_reset_date,
            block_reason=block_reason,
            error=status_obj.error,
            details={
                "premium_percent_remaining": status_obj.premium_percent_remaining,
                "limit_reached": status_obj.limit_reached,
            },
        )

    if provider == "zai":
        status_obj = quota.check_zai_quota()
        block_reason = quota.zai_quota_block_reason()
        windows = {
            "api_calls": status_obj.api_calls,
            "tokens": status_obj.tokens,
        }
        selected_name, selected_window = _pick_named_window(windows)
        status = "error" if status_obj.error else "blocked" if block_reason else "ok"
        used = selected_window.limit - selected_window.remaining
        return _usage_window_record(
            provider=provider,
            status=status,
            used=None if status_obj.error else used,
            limit=None if status_obj.error else selected_window.limit,
            remaining=None if status_obj.error else selected_window.remaining,
            unit=selected_name,
            reset_window=f"{selected_window.window_hours}h" if selected_window.window_hours else selected_name,
            reset_at=None,
            block_reason=block_reason,
            error=status_obj.error,
            details={
                "limit_reached": status_obj.limit_reached,
                "max_used_percent": status_obj.max_used_percent,
                "windows": {
                    "api_calls": asdict(status_obj.api_calls),
                    "tokens": asdict(status_obj.tokens),
                },
            },
        )

    raise _usage_provider_error(provider)


def _format_usage_line(record: dict[str, Any]) -> str:
    fields = [
        record["provider"] + ":",
        f"status={record['status']}",
        f"used={record['used'] if record['used'] is not None else 'unknown'}",
        f"limit={record['limit'] if record['limit'] is not None else 'unknown'}",
        f"remaining={record['remaining'] if record['remaining'] is not None else 'unknown'}",
        f"unit={record['unit'] or 'unknown'}",
        f"reset_window={record['reset_window'] or 'unknown'}",
        f"reset_at={record['reset_at'] or 'unknown'}",
    ]
    if record["block_reason"]:
        fields.append(f"block_reason={record['block_reason']}")
    if record["error"]:
        fields.append(f"error={record['error']}")
    return " ".join(fields)


def _render_usage(records: list[dict[str, Any]], *, json_output: bool) -> None:
    if json_output:
        if len(records) == 1:
            typer.echo(json.dumps(records[0], sort_keys=True))
            return
        for record in records:
            typer.echo(json.dumps(record, sort_keys=True))
        return

    for record in records:
        typer.echo(_format_usage_line(record))


def _run_engine(
    engine_name: str,
    prompt: str,
    cwd: Path,
    *,
    model: str | None = None,
    max_turns: int | None = None,
    resume: str | None = None,
    continue_latest: bool = False,
    raw: bool = False,
) -> int:
    engine = get_engine(engine_name)
    if resume is not None and continue_latest:
        raise click.ClickException("Cannot combine --resume with --continue.")
    if continue_latest:
        if not engine.supports_continue_latest():
            raise click.ClickException(
                f"{engine_name} does not support --continue; pass --resume <id> instead."
            )
        resume = LATEST_CONTINUATION_SENTINEL
    run_kwargs = {
        "model": model,
        "max_turns": max_turns,
        "resume_session_id": resume,
    }
    if "emit_unified" in inspect.signature(engine.run).parameters:
        run_kwargs["emit_unified"] = not raw
    execution = engine.run(prompt, cwd.resolve(), **run_kwargs)
    if execution.stdout:
        sys.stdout.write(execution.stdout)
    if execution.stderr:
        sys.stderr.write(execution.stderr)
    return execution.exit_code


PromptArgument = Annotated[str, typer.Argument(help="Prompt to send to the selected engine.")]
CwdOption = Annotated[Path, typer.Option(help="Working directory for the engine subprocess.")]
ModelOption = Annotated[str | None, typer.Option(help="Override the engine model name.")]
MaxTurnsOption = Annotated[int | None, typer.Option(help="Limit the number of turns for the run.")]
ResumeOption = Annotated[
    str | None,
    typer.Option(help="Resume a prior engine session by continuation or session ID."),
]
ContinueOption = Annotated[
    bool,
    typer.Option("--continue", help="Resume the most recent session for this engine."),
]
RawOption = Annotated[bool, typer.Option(help="Emit the engine's raw JSON/JSONL output.")]


def _engine_command_factory(engine_name: str):
    def command(
        prompt: PromptArgument,
        cwd: CwdOption = Path.cwd(),
        model: ModelOption = None,
        max_turns: MaxTurnsOption = None,
        resume: ResumeOption = None,
        continue_: ContinueOption = False,
        raw: RawOption = False,
    ) -> int:
        return _run_engine(
            engine_name,
            prompt,
            cwd,
            model=model,
            max_turns=max_turns,
            resume=resume,
            continue_latest=continue_,
            raw=raw,
        )

    command.__name__ = f"{engine_name}_command"
    command.__doc__ = f"Run a prompt with the {engine_name} adapter."
    return command


for _engine_name in ENGINE_CHOICES:
    app.command(_engine_name)(_engine_command_factory(_engine_name))


@app.command("usage")
def usage_command(
    provider: Annotated[str | None, typer.Argument(help="Optional provider name.")] = None,
    json_: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON output.")] = False,
) -> int:
    selected_providers = (
        [provider] if provider is not None else list(SUPPORTED_USAGE_PROVIDERS)
    )
    for name in selected_providers:
        if name not in USAGE_PROVIDER_CHOICES:
            raise _usage_provider_error(name)
    records = [_normalize_usage_provider(name) for name in selected_providers]
    _render_usage(records, json_output=json_)
    return 0


def build_legacy_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="heru", add_help=False)
    parser.add_argument("prompt")
    parser.add_argument("--engine", choices=ENGINE_CHOICES, required=True)
    parser.add_argument("--model")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--resume")
    parser.add_argument("--resume-session-id")
    parser.add_argument("--continue", dest="continue_", action="store_true")
    parser.add_argument("--raw", action="store_true")
    return parser


def _is_legacy_invocation(argv: list[str]) -> bool:
    return "--engine" in argv


def _run_legacy_cli(argv: list[str]) -> int:
    args = build_legacy_parser().parse_args(argv)
    resume = args.resume if args.resume is not None else args.resume_session_id
    print(
        "Deprecation warning: `heru <prompt> --engine <engine>` is deprecated; "
        "use `heru <engine> <prompt>` instead.",
        file=sys.stderr,
    )
    return _run_engine(
        args.engine,
        args.prompt,
        args.cwd,
        model=args.model,
        max_turns=args.max_turns,
        resume=resume,
        continue_latest=args.continue_,
        raw=args.raw,
    )


def main(argv: list[str] | None = None) -> int:
    """Public CLI entrypoint preserving heru's supported argv contract."""
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if _is_legacy_invocation(effective_argv):
            return _run_legacy_cli(effective_argv)
        result = app(
            args=effective_argv,
            prog_name="heru",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return exc.exit_code
    return 0 if result is None else int(result)


if __name__ == "__main__":
    raise SystemExit(main())
