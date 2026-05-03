"""Daemon lifecycle helpers for Litehive pool execution.

The daemon's public surface lives in its named submodules
(``daemon.execution``, ``daemon.registry``, ``daemon.logs``); this
package init intentionally has no behavior. Re-exporting from
submodules here would force importing one daemon submodule to also
import the others (importing ``daemon.logs`` would run
``daemon.execution`` as a side-effect of package init), creating
import cycles with ``observability.status``. Keep this file empty.
"""
