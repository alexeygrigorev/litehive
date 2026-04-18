"""Compatibility wrappers for Heru continuation helpers."""

from heru import extract_engine_continuation as _extract_engine_continuation


def extract_engine_continuation(engine_name, execution):
    return _extract_engine_continuation(engine_name, execution)


def extract_execution_continuation(engine_name, execution):
    return extract_engine_continuation(engine_name, execution)


__all__ = ["extract_engine_continuation", "extract_execution_continuation"]
