"""Workspace observability context.

Observability is one bounded context for read-only operational signals:
task/status rendering, status diagnostics, lifecycle events, engine monitoring,
and environment health. Modules here may read task, state, and config data to
explain current behavior, but they should not repair or mutate workspace state.
"""
