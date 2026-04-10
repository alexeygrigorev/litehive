"""Standalone heru CLI entrypoint."""

import argparse
import sys
from pathlib import Path

from heru import get_engine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="heru")
    parser.add_argument("prompt")
    parser.add_argument("--engine", required=True)
    parser.add_argument("--model")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--resume-session-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine = get_engine(args.engine)
    execution = engine.run(
        args.prompt,
        args.cwd.resolve(),
        model=args.model,
        max_turns=args.max_turns,
        resume_session_id=args.resume_session_id,
    )
    if execution.stdout:
        sys.stdout.write(execution.stdout)
    if execution.stderr:
        sys.stderr.write(execution.stderr)
    return execution.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
