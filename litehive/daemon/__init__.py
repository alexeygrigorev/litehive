"""
Daemon lifecycle for Litehive pool execution.

Public surface is split across named submodules so callers can
import only what they need:

- ``daemon.execution`` — the daemon loop body, start/stop, and the
  status-snapshot hooks the loop uses every iteration.
- ``daemon.registry`` — the lockfile-backed registration and
  heartbeat that lets the operator discover the live daemon for a
  workspace.
- ``daemon.logs`` — pruning and discovery of run-all session log
  directories so the operator can ``tail -f`` the latest run.

The init is intentionally empty: re-exporting submodule symbols
here would force importing one submodule to also import the others
(loading ``daemon.logs`` would pull in ``daemon.execution``) and
that creates import cycles with ``observability.status``.
"""
