"""Task application boundary.

Ownership inside this package is intentionally split by task concern:

- ``status`` and ``runtime`` mutate task lifecycle/application state.
- ``queue`` owns runnable task selection and queue ordering.
- ``activity`` owns persisted task activity entries.
- ``report_storage``, ``recovery_reports``, ``recovery_evidence``, and
  ``activity_rendering`` own report persistence, recovery context, and display.
- ``paths`` owns task-local filesystem and artifact path lookup.

Keep cross-cutting orchestration in callers or explicit services; this package
should not regain a single catch-all task module.
"""
